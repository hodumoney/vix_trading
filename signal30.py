#!/usr/bin/env python3
"""
빅스 매수법 알림 (텔레그램) — 규칙 v2

규칙
  1) 신호(D): VIX 종가 >= 30 이고 전날 종가 < 30
  2) 매수: 다음 거래일(D+1) 미국장 시가
  3) 판단: 2거래일 뒤(D+2) 종가 기준 (한국시간 D+3 아침에 알림)
       이익이면                                   -> D+3 시가 매도
       손실이어도 ① VIX(D+2) < 돌파일 VIX
                  ② QQQ가 52주 최고 종가 대비 -20% 이내  -> 계속 보유(연장)
       그 밖의 손실                               -> D+3 시가 매도
  4) 연장 중: 매일 종가 VIX < 25 이면 다음 거래일 시가 매도. 늦어도 D+18 시가 매도
  5) 보유 마지막 날 종가에 VIX가 다시 30을 새로 돌파하면 판단을 2거래일 미룸

매일 한국시간 아침 7:40(10:10 재확인)에 실행되고, 아래 일이 있는 날에만 보냅니다.
  매수 신호 / 오늘 밤 매수 / 판단(매도 또는 연장) / 연장 중 매일 계속 보유 / 매도 / 오늘 밤 매도
이미 보낸 알림은 signal30_state.json 에 기록해 두 번 보내지 않습니다.
수동 실행(MODE=status)하면 지금 상태를 바로 보냅니다(테스트용).

환경변수
  TG_TOKEN, TG_CHAT_ID   텔레그램 봇
  ETF                    매수할 ETF (기본 TQQQ). 손익 계산에도 이 ETF 가격을 씀
  MODE                   run(기본) / status
  OFFLINE_CSV, OFFLINE_PX_DIR, ASOF, NOW_KST   (테스트용) 인터넷 대신 CSV, 특정 날짜 기준 실행
표준 라이브러리만 사용. Python 3.9+
"""
import csv, io, json, os, sys, datetime as dt, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "signal30_state.json")
TAG = "[빅스 매수법]"
THRESHOLD = float(os.environ.get("VIX_THRESHOLD") or 30)   # 돌파 기준
HOLD = int(os.environ.get("HOLD_DAYS") or 2)               # 기본 보유 거래일
CALM = float(os.environ.get("VIX_CALM") or 25)             # 연장 중 매도 기준
MAX_EXT = int(os.environ.get("MAX_EXT_DAYS") or 15)        # 연장 최대 거래일 (D+3 + 15 = D+18)
BEAR = -0.20                                               # QQQ 52주 고점 대비 약세장 기준
FEE = 0.0015                                               # 매수 수수료(손익 판단용)
ETF = (os.environ.get("ETF") or "TQQQ").upper()
NY, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
WD = "월화수목금토일"

# NYSE 휴장일 (공식 일정 2025~2028). 해마다 nyse.com/trade/hours-calendars 에서 확인해 추가하세요.
HOLIDAYS = {dt.date.fromisoformat(s) for s in """
2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25
2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24
2028-01-17 2028-02-21 2028-04-14 2028-05-29 2028-06-19 2028-07-04 2028-09-04 2028-11-23 2028-12-25
""".split()}


def is_session(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def next_sessions(d, k):
    out = []
    while len(out) < k:
        d += dt.timedelta(days=1)
        if is_session(d):
            out.append(d)
    return out


# ---------------- 데이터 ----------------
def _get(url, t=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return r.read().decode("utf-8", "replace")


def _yahoo(sym, rng):
    j = json.loads(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range={rng}&interval=1d"))
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = {}
    for ts, o, c in zip(res["timestamp"], q["open"], q["close"]):
        if c is not None:
            out[dt.datetime.fromtimestamp(ts, NY).date()] = (float(o) if o is not None else None, float(c))
    return out


def _stooq(sym):
    out = {}
    for row in csv.DictReader(io.StringIO(_get(f"https://stooq.com/q/d/l/?s={sym}&i=d"))):
        try:
            out[dt.date.fromisoformat(row["Date"])] = (float(row["Open"]), float(row["Close"]))
        except (KeyError, ValueError, TypeError):
            pass
    return out


def _cboe_vix():
    out = {}
    for row in csv.DictReader(io.StringIO(_get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"))):
        try:
            out[dt.datetime.strptime(row["DATE"], "%m/%d/%Y").date()] = (float(row["OPEN"]), float(row["CLOSE"]))
        except (KeyError, ValueError):
            pass
    return out


def _csv(path):
    """테스트용: DATE(YYYY-MM-DD 또는 MM/DD/YYYY), CLOSE (선택 OPEN) 열이 있는 CSV."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().upper(): v for k, v in row.items()}
            s = row.get("DATE")
            try:
                d = dt.date.fromisoformat(s) if "-" in s else dt.datetime.strptime(s, "%m/%d/%Y").date()
                o = float(row["OPEN"]) if row.get("OPEN") else None
                out[d] = (o, float(row["CLOSE"]))
            except (TypeError, ValueError, KeyError):
                pass
    return out


def _best(sources, min_len):
    best, src = {}, None
    for name, fn in sources:
        try:
            m = fn()
        except Exception as e:  # 소스 하나가 막혀도 다음으로
            print(f"  {name} 실패: {e}", file=sys.stderr)
            continue
        if len(m) >= min_len and (not best or max(m) > max(best)):
            best, src = m, name
    return best, src


def _trim(series, now_ny):
    asof = dt.date.fromisoformat(os.environ["ASOF"]) if os.environ.get("ASOF") else None
    days = sorted(d for d in series if is_session(d) and (asof is None or d <= asof))
    # 오늘 장이 아직 안 끝났으면(16:20 ET 전) 오늘 값은 장중값이라 제외
    if asof is None and days and days[-1] == now_ny.date() and now_ny.time() < dt.time(16, 20):
        days = days[:-1]
    return days


def load_vix(now_ny):
    if os.environ.get("OFFLINE_CSV"):
        series, src = _csv(os.environ["OFFLINE_CSV"]), "CSV"
    else:
        series, src = _best((("Yahoo", lambda: _yahoo("^VIX", "1y")),
                             ("Stooq", lambda: _stooq("^vix")),
                             ("CBOE", _cboe_vix)), 30)
    if not series:
        raise SystemExit("VIX 데이터를 가져오지 못했습니다.")
    days = _trim(series, now_ny)
    return days, [series[d][1] for d in days], src


def load_px(sym, now_ny):
    """{날짜: (시가, 종가)}. 못 받으면 빈 dict (판단은 보류하고 경고)."""
    if os.environ.get("OFFLINE_PX_DIR"):
        p = os.path.join(os.environ["OFFLINE_PX_DIR"], f"{sym}.csv")
        series = _csv(p) if os.path.exists(p) else {}
    else:
        series, _ = _best((("Yahoo", lambda: _yahoo(sym, "2y")),
                           ("Stooq", lambda: _stooq(f"{sym.lower()}.us"))), 200)
    return {d: series[d] for d in _trim(series, now_ny)}


# ---------------- 규칙 ----------------
def replay(closes):
    """신호·매수·원래 매도(판단 다음 날) 인덱스. 보유 마지막 날 새 돌파면 2일 미룸."""
    n, trades, i = len(closes), [], 1
    while i < n:
        if closes[i] >= THRESHOLD and closes[i - 1] < THRESHOLD:
            a = i + 1
            if trades and trades[-1]["sell"] == a:      # 보유 마지막 날 새 돌파 -> 2일 미룸
                trades[-1]["sell"] = a + HOLD
                trades[-1]["ext"].append(i)
            else:
                trades.append({"sig": i, "buy": a, "sell": a + HOLD, "ext": []})
            i = a + HOLD - 1
        else:
            i += 1
    return trades


def pnl_at(tr, k, day, etf):
    """k일 종가 기준 손익(매수일 시가 대비, 매수 수수료 포함). 데이터 없으면 None."""
    b, c = etf.get(day(tr["buy"])), etf.get(day(k))
    if not b or not c or not b[0]:
        return None
    return c[1] / b[0] * (1 - FEE) - 1


def qqq_dd(d, qqq):
    """d일 QQQ 종가의 52주(252거래일) 최고 종가 대비 하락률."""
    ks = sorted(k for k in qqq if k <= d)[-252:]
    if len(ks) < 200 or ks[-1] != d:
        return None
    return qqq[d][1] / max(qqq[k][1] for k in ks) - 1


def analyze(days, closes, etf, qqq):
    """각 거래에 판단(dec)·매도 인덱스(x)·이유(why)·기한(deadline)을 붙임. x=None이면 아직 보유/미정."""
    n = len(days)
    fut = next_sessions(days[-1], 60)
    day = lambda k: days[k] if k < n else fut[k - n]
    trades = replay(closes)
    for tr in trades:
        e = tr["sell"]
        tr.update(e=e, x=None, why=None, dec=None, deadline=e + MAX_EXT)
        last = e - 1
        if last > n - 1:                     # 아직 판단할 종가가 없음
            continue
        pnl, dd = pnl_at(tr, last, day, etf), qqq_dd(days[last], qqq)
        if pnl is None or dd is None:        # 가격 데이터 없음 -> 판단 보류
            continue
        c1, c2 = closes[last] < closes[tr["sig"]], dd > BEAR
        tr["dec"] = dict(pnl=pnl, dd=dd, c1=c1, c2=c2, extend=pnl < 0 and c1 and c2)
        if not tr["dec"]["extend"]:
            tr["x"], tr["why"] = e, ("profit" if pnl >= 0 else "loss")
            continue
        for t in range(e, min(e + MAX_EXT, n)):
            if closes[t] < CALM:
                tr["x"], tr["why"] = t + 1, "calm"
                break
        else:
            if e + MAX_EXT - 1 <= n - 1:
                tr["x"], tr["why"] = e + MAX_EXT, "deadline"
    return trades, day


def end_of(tr):
    """이 거래로 보유하는 마지막 시점(매도 시가 인덱스, 미정이면 최대치)."""
    if tr["x"] is not None:
        return tr["x"]
    return tr["deadline"] if tr["dec"] and tr["dec"]["extend"] else tr["e"]


def held_by_other(tr, trades, k):
    """k번째 거래일 시가에 다른 거래로 보유가 이어지는지 (새 신호와 겹칠 때)."""
    return any(t is not tr and t["buy"] <= k < end_of(t) for t in trades)


# ---------------- 메시지 ----------------
def fmt_day(d):
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def open_kst(d):
    t = dt.datetime.combine(d, dt.time(9, 30), NY).astimezone(KST)
    return f"한국시간 {t.month}/{t.day} {t:%H:%M}"


def pct(x):
    return f"{x * 100:+.1f}%".replace("-", "−")


def when(d, today):
    return "오늘 밤" if d == today else fmt_day(d)


def sell_block(title, reason_lines, d_sell, today, overlap):
    if overlap:
        return [f"⏳ {TAG} 계속 보유 (다른 신호 보유 중)", *reason_lines,
                "→ 다른 신호로 보유가 이어지니 팔지 말고 계속 보유", "※ 매도일은 그 신호 기준으로 다시 알려드립니다."]
    head = f"🔔 {TAG} {'오늘 밤 ' if d_sell == today else ''}매도 ({title})"
    return [head, *reason_lines,
            f"→ {when(d_sell, today)} 미국장 시가 매도 · {open_kst(d_sell)}",
            "→ 토스에서 시장가 예약주문(매도)"]


def signal_msg(tr, days, closes, day, today, holding):
    s = tr["sig"]
    lines = [f"🚨 {TAG} 매수 신호",
             f"{fmt_day(days[s])} VIX 종가 {closes[s]:.2f} (전날 {closes[s - 1]:.2f}) → {THRESHOLD:g} 새로 돌파"]
    if holding:
        lines += ["이미 보유 중 → 추가 매수하지 말고 그대로 보유하세요."]
    else:
        lines += [f"매수: {fmt_day(day(tr['buy']))} 미국장 시가 · {open_kst(day(tr['buy']))}",
                  f"→ 토스에서 {ETF} 시장가 예약주문(매수)"]
    lines.append(f"※ {fmt_day(day(tr['e']))} 아침에 매도할지, 더 들고 갈지 알려드립니다.")
    return "\n".join(lines)


def chain_msg(tr, days, closes, day, L):
    return "\n".join([
        f"↪️ {TAG} 보유 연장 (재돌파)",
        f"{fmt_day(days[L])} VIX 종가 {closes[L]:.2f} (전날 {closes[L - 1]:.2f}) → 보유 마지막 날 다시 {THRESHOLD:g} 돌파",
        f"→ {fmt_day(day(L + 1))} 시가에 팔지 말고 계속 보유",
        f"※ {fmt_day(day(tr['e']))} 아침에 매도할지, 더 들고 갈지 알려드립니다.",
    ])


def decision_msg(tr, trades, days, closes, day, today):
    dec, e, s = tr["dec"], tr["e"], tr["sig"]
    last = e - 1
    head = f"{fmt_day(days[last])} 종가 {ETF} {pct(dec['pnl'])} (매수일 시가 대비)"
    cond = [f"① VIX {closes[last]:.2f} {'<' if dec['c1'] else '≥'} 돌파일 {closes[s]:.2f}  {'✅' if dec['c1'] else '❌'}",
            f"② QQQ 52주 고점 대비 {pct(dec['dd'])}  {'✅' if dec['c2'] else '❌ 약세장'}"]
    if dec["extend"]:
        dl = day(tr["deadline"])
        return "\n".join([f"↪️ {TAG} 보유 연장", head, *cond,
                          f"→ {when(day(e), today)} 팔지 말고 계속 보유",
                          f"매도: VIX 종가가 {CALM:g} 아래로 내려오면 다음 날 시가 (매일 아침 알려드림)",
                          f"기한: {fmt_day(dl)} 시가 · {open_kst(dl)}"])
    overlap = held_by_other(tr, trades, e)
    if dec["pnl"] >= 0:
        return "\n".join(sell_block("이익", [head], day(e), today, overlap))
    return "\n".join(sell_block("손실", [head, *cond, "연장 조건 안 맞음 → 예정대로 매도"], day(e), today, overlap))


def hold_msg(tr, days, closes, day, etf, L, dl):
    lines = [f"⏳ {TAG} 계속 보유", f"{fmt_day(days[L])} VIX 종가 {closes[L]:.2f} → {CALM:g} 이상"]
    p = pnl_at(tr, L, day, etf)
    if p is not None:
        lines.append(f"{ETF} {pct(p)} (매수일 시가 대비)")
    lines.append(f"→ 오늘은 할 일 없음 · 기한 {fmt_day(day(dl))} 시가")
    return "\n".join(lines)


def exit_msg(tr, trades, days, closes, day, etf, L, today):
    if tr["why"] == "calm":
        title, why = "공포 진정", f"{fmt_day(days[L])} VIX 종가 {closes[L]:.2f} → {CALM:g} 미만"
    else:
        title, why = "기한", f"{fmt_day(days[L])} VIX 종가 {closes[L]:.2f} → {CALM:g} 이상이지만 기한 도달"
    lines = [why]
    p = pnl_at(tr, L, day, etf)
    if p is not None:
        lines.append(f"{ETF} {pct(p)} (매수일 시가 대비)")
    return "\n".join(sell_block(title, lines, day(tr["x"]), today, held_by_other(tr, trades, tr["x"])))


WHY = {"profit": "2거래일 뒤 이익", "loss": "손실, 연장 조건 안 맞음",
       "calm": f"VIX {CALM:g} 미만 (공포 진정)", "deadline": "연장 기한 도달"}


def events(days, closes, etf, qqq, today, hour):
    """(키, 메시지, 함께 처리할 키들) 목록."""
    n, L = len(days), len(days) - 1
    trades, day = analyze(days, closes, etf, qqq)
    out = []
    # 1) 새 종가(L)로 생기는 일
    for tr in trades:
        s, e = tr["sig"], tr["e"]
        if s == L:
            holding = held_by_other(tr, trades, tr["buy"])
            extra = [f"buy:{day(tr['buy'])}"] if (holding or day(tr["buy"]) == today) else []
            out.append((f"signal:{days[s]}", signal_msg(tr, days, closes, day, today, holding), extra))
        if L in tr["ext"]:
            out.append((f"chain:{days[L]}", chain_msg(tr, days, closes, day, L), []))
        if e - 1 == L:
            if tr["dec"]:
                extra = [f"sell:{day(e)}"] if not tr["dec"]["extend"] and day(e) == today else []
                out.append((f"decide:{days[s]}", decision_msg(tr, trades, days, closes, day, today), extra))
            elif hour >= 10:   # 재확인 때까지 가격을 못 받으면 경고
                out.append((f"decidefail:{days[s]}", "\n".join([
                    f"⚠️ {TAG} 판단 보류 (가격 데이터 못 받음)",
                    f"{fmt_day(days[L])} VIX 종가 {closes[L]:.2f} (돌파일 {closes[s]:.2f})",
                    f"직접 확인: {ETF}가 매수일 시가보다 낮고, VIX < 돌파일, QQQ 52주 고점 대비 −20% 이내면 계속 보유",
                    f"아니면 {when(day(e), today)} 시가 매도 · {open_kst(day(e))}"]), []))
        if tr["dec"] and tr["dec"]["extend"] and e <= L < tr["deadline"]:
            if tr["x"] == L + 1:
                extra = [f"sell:{day(L + 1)}"] if day(L + 1) == today else []
                out.append((f"exit:{day(L + 1)}", exit_msg(tr, trades, days, closes, day, etf, L, today), extra))
            elif tr["x"] is None:
                dl = max(t["deadline"] for t in trades if t["dec"] and t["dec"]["extend"] and t["buy"] <= L + 1 < end_of(t))
                out.append((f"hold:{days[L]}", hold_msg(tr, days, closes, day, etf, L, dl), []))
    # 2) 매수일·매도일 당일 아침 알림 (어제 종가까지 들어왔을 때만)
    if is_session(today):
        prev = today - dt.timedelta(days=1)
        while not is_session(prev):
            prev -= dt.timedelta(days=1)
        if days[-1] == prev:
            for tr in trades:
                if tr["buy"] == n and not held_by_other(tr, trades, n):
                    out.append((f"buy:{today}", "\n".join([
                        f"⏰ {TAG} 오늘 밤 매수",
                        f"오늘 밤 미국장 시가에 {ETF} 매수 · {open_kst(today)}",
                        f"신호: {fmt_day(days[tr['sig']])} VIX 종가 {closes[tr['sig']]:.2f}",
                        f"※ {fmt_day(day(tr['e']))} 아침에 매도할지, 더 들고 갈지 알려드립니다.",
                        "→ 토스에서 시장가 예약주문(매수)"]), []))
                if tr["x"] == n and not held_by_other(tr, trades, n):
                    out.append((f"sell:{today}", "\n".join([
                        f"🔔 {TAG} 오늘 밤 매도",
                        f"오늘 밤 미국장 시가에 {ETF} 매도 · {open_kst(today)}",
                        f"이유: {WHY[tr['why']]}",
                        "→ 토스에서 시장가 예약주문(매도)"]), []))
    return out


def status(days, closes, src, etf, qqq, today):
    n = len(days)
    trades, day = analyze(days, closes, etf, qqq)
    lines = [f"📋 {TAG} 현재 상태",
             f"마지막 VIX 종가: {fmt_day(days[-1])} {closes[-1]:.2f} (출처 {src})",
             f"규칙: VIX 종가 {THRESHOLD:g} 새로 돌파 → 다음 날 시가 {ETF} 매수 → 2거래일 뒤 종가로 판단",
             f"  이익이면 매도 / 손실이어도 VIX가 돌파일보다 낮고 QQQ 고점 대비 −20% 이내면 보유",
             f"  보유 중엔 VIX 종가 {CALM:g} 미만인 다음 날 시가 매도, 늦어도 D+18"]
    act = [t for t in trades if t["buy"] <= n and end_of(t) >= n]
    if not act:
        lines.append(f"보유 없음. VIX가 {THRESHOLD:g} 아래에서 위로 종가 마감하는 날 알려드립니다.")
    for tr in act:
        p = pnl_at(tr, n - 1, day, etf) if tr["buy"] < n else None
        pt = f" · {ETF} {pct(p)}" if p is not None else ""
        if tr["buy"] == n:
            lines.append(f"매수 대기: {fmt_day(day(tr['buy']))} 시가 매수 · 판단 {fmt_day(day(tr['e']))} 아침")
        elif tr["x"] is not None:
            lines.append(f"매도 예정: {fmt_day(day(tr['x']))} 시가 · 이유: {WHY[tr['why']]}{pt}")
        elif tr["dec"] and tr["dec"]["extend"]:
            lines.append(f"보유(연장) 중: {fmt_day(day(tr['buy']))} 매수{pt} · 기한 {fmt_day(day(tr['deadline']))} 시가")
        else:
            lines.append(f"보유 중: {fmt_day(day(tr['buy']))} 매수{pt} · 판단 {fmt_day(day(tr['e']))} 아침")
    recent = [t for t in trades if t["sig"] >= n - 250][-3:]
    if recent:
        lines.append("최근 신호: " + ", ".join(f"{fmt_day(days[t['sig']])} {closes[t['sig']]:.1f}" for t in recent))
    if not etf or not qqq:
        lines.append(f"⚠️ 가격 데이터({ETF if not etf else 'QQQ'})를 못 받아 손익 판단을 못 합니다.")
    return "\n".join(lines)


# ---------------- 발송 ----------------
def send(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("(TG_TOKEN/TG_CHAT_ID 없음 - 미리보기만)\n" + text + "\n")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30) as r:
        resp = json.loads(r.read().decode())
    if not resp.get("ok"):
        raise SystemExit(f"텔레그램 발송 실패: {resp}")
    print("발송 완료:", text.splitlines()[0])


def run(days, closes, etf, qqq, now_kst, state):
    """보낼 메시지 목록과 갱신된 state."""
    today = now_kst.date()
    sent = set(state.get("sent", []))
    msgs = []
    for key, msg, extra in events(days, closes, etf, qqq, today, now_kst.hour):
        if key in sent:
            continue
        msgs.append(msg)
        sent.add(key)
        sent.update(extra)
    keep = (today - dt.timedelta(days=90)).isoformat()
    state["sent"] = sorted(k for k in sent if k.split(":", 1)[-1] >= keep)
    state["last_processed"] = days[-1].isoformat()
    state["last_vix"] = round(closes[-1], 2)
    return msgs, state


def main():
    now_ny = dt.datetime.now(NY)
    now_kst = dt.datetime.fromisoformat(os.environ["NOW_KST"]).replace(tzinfo=KST) if os.environ.get("NOW_KST") else dt.datetime.now(KST)
    days, closes, src = load_vix(now_ny)
    etf, qqq = load_px(ETF, now_ny), load_px("QQQ", now_ny)
    print(f"VIX {len(days)}일, 마지막 {days[-1]} {closes[-1]:.2f} ({src}) · {ETF} {len(etf)}일 · QQQ {len(qqq)}일 · 지금 한국시간 {now_kst:%m/%d %H:%M}")
    if (os.environ.get("MODE") or "run") == "status":
        send(status(days, closes, src, etf, qqq, now_kst.date()))
        return
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    msgs, state = run(days, closes, etf, qqq, now_kst, state)
    state["source"] = src
    for m in msgs:
        send(m)
    if not msgs:
        print("오늘은 보낼 알림 없음")
    if not os.environ.get("ASOF") or os.environ.get("WRITE_STATE"):
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
