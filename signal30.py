#!/usr/bin/env python3
"""
VIX 30 돌파 단기매매 알림 (텔레그램)

규칙
  1) 오늘 VIX 종가 >= 30 이고 어제 종가 < 30  ->  신호
  2) 다음 미국 거래일 시가에 매수
  3) 2거래일 뒤 시가에 매도
  4) 보유 마지막 날 종가에 새 돌파가 나오면 팔지 않고 2일 연장

매 거래일 미국장 마감 뒤(한국시간 아침 7:40, 10:10 재확인) 실행되고,
아래 일이 있는 날에만 텔레그램을 보냅니다.
  - 매수 신호   (오늘 밤 시가에 매수)
  - 매도일      (오늘 밤 시가에 매도)
  - 보유 연장   (오늘 밤 팔지 말 것)
이미 보낸 날짜는 signal30_state.json 에 기록해 같은 알림을 두 번 보내지 않습니다.

수동 실행(MODE=status)하면 지금 상태를 바로 보냅니다(테스트용).

환경변수
  TG_TOKEN, TG_CHAT_ID   텔레그램 봇 (기존 VIX 적립 알림과 같은 값)
  ETF                    매수할 ETF (기본 TQQQ)
  MODE                   run(기본) / status
  OFFLINE_CSV, ASOF      (테스트용) 인터넷 대신 CSV로, 특정 날짜 기준으로 실행
표준 라이브러리만 사용. Python 3.9+
"""
import csv, io, json, os, sys, datetime as dt, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "signal30_state.json")
THRESHOLD = float(os.environ.get("VIX_THRESHOLD") or 30)
HOLD = int(os.environ.get("HOLD_DAYS") or 2)
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


# ---------------- VIX 종가 ----------------
def _get(url, t=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return r.read().decode("utf-8", "replace")


def from_yahoo():
    j = json.loads(_get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=6mo&interval=1d"))
    res = j["chart"]["result"][0]
    out = {}
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[dt.datetime.fromtimestamp(ts, NY).date()] = float(c)
    return out


def from_stooq():
    out = {}
    for row in csv.DictReader(io.StringIO(_get("https://stooq.com/q/d/l/?s=^vix&i=d"))):
        try:
            out[dt.date.fromisoformat(row["Date"])] = float(row["Close"])
        except (KeyError, ValueError):
            pass
    return out


def from_cboe():
    out = {}
    for row in csv.DictReader(io.StringIO(_get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"))):
        try:
            out[dt.datetime.strptime(row["DATE"], "%m/%d/%Y").date()] = float(row["CLOSE"])
        except (KeyError, ValueError):
            pass
    return out


def from_csv(path):
    """테스트용: DATE(YYYY-MM-DD 또는 MM/DD/YYYY), CLOSE 열이 있는 CSV."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().upper(): v for k, v in row.items()}
            s = row.get("DATE")
            try:
                d = dt.date.fromisoformat(s) if "-" in s else dt.datetime.strptime(s, "%m/%d/%Y").date()
                out[d] = float(row["CLOSE"])
            except (TypeError, ValueError):
                pass
    return out


def load_vix(now_ny):
    if os.environ.get("OFFLINE_CSV"):
        series, src = from_csv(os.environ["OFFLINE_CSV"]), "CSV"
    else:
        best, src = {}, None
        for name, fn in (("Yahoo", from_yahoo), ("Stooq", from_stooq), ("CBOE", from_cboe)):
            try:
                m = fn()
            except Exception as e:  # 소스 하나가 막혀도 다음으로
                print(f"  {name} 실패: {e}", file=sys.stderr)
                continue
            if len(m) >= 30 and (not best or max(m) > max(best)):
                best, src = m, name
        series = best
    if not series:
        raise SystemExit("VIX 데이터를 가져오지 못했습니다.")
    asof = dt.date.fromisoformat(os.environ["ASOF"]) if os.environ.get("ASOF") else None
    days = sorted(d for d in series if is_session(d) and (asof is None or d <= asof))
    # 오늘 장이 아직 안 끝났으면(16:20 ET 전) 오늘 값은 장중값이라 제외
    if asof is None and days and days[-1] == now_ny.date() and now_ny.time() < dt.time(16, 20):
        days = days[:-1]
    return days, [series[d] for d in days], src


# ---------------- 규칙 재현 ----------------
def replay(closes):
    """[{sig, buy, sell, ext:[...]}] — 인덱스는 closes 기준(len 이상이면 미래 거래일)."""
    n, trades, i = len(closes), [], 1
    while i < n:
        if closes[i] >= THRESHOLD and closes[i - 1] < THRESHOLD:
            a = i + 1
            if trades and trades[-1]["sell"] == a:      # 보유 마지막 날 새 돌파 -> 연장
                trades[-1]["sell"] = a + HOLD
                trades[-1]["ext"].append(i)
            else:
                trades.append({"sig": i, "buy": a, "sell": a + HOLD, "ext": []})
            i = a + HOLD - 1
        else:
            i += 1
    return trades


def fmt_day(d):
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def open_kst(d):
    t = dt.datetime.combine(d, dt.time(9, 30), NY).astimezone(KST)
    return f"한국시간 {t.month}/{t.day} {t:%H:%M}"


def active_trade(days, closes):
    """지금 진행 중이거나(보유) 곧 시작할(내일 매수) 거래와, 인덱스->날짜 함수."""
    n = len(days)
    fut = next_sessions(days[-1], 12)
    day = lambda k: days[k] if k < n else fut[k - n]
    trades = replay(closes)
    act = next((t for t in trades if t["sell"] >= n and t["buy"] <= n), None)
    return trades, act, day


def buy_msg(days, closes, tr, day):
    return "\n".join([
        "\U0001F6A8 [VIX 30 돌파] 매수 신호",
        f"{fmt_day(days[tr['sig']])} VIX 종가 {closes[tr['sig']]:.2f} (전날 {closes[tr['sig'] - 1]:.2f}) → {THRESHOLD:g} 새로 돌파",
        f"매수: {fmt_day(day(tr['buy']))} 미국장 시가 · {open_kst(day(tr['buy']))}",
        f"매도 예정: {fmt_day(day(tr['sell']))} 미국장 시가 · {open_kst(day(tr['sell']))}",
        f"→ 토스에서 {ETF} 시장가 예약주문(매수)",
        f"※ 매도일 아침에 다시 알려드립니다. 보유 마지막 날 다시 돌파하면 {HOLD}일 연장.",
    ])


def new_close_events(days, closes):
    """새 종가가 들어온 날 한 번: 매수 신호 / 보유 연장."""
    n = len(days)
    trades, act, day = active_trade(days, closes)
    out = []
    for tr in trades:
        if tr["sig"] == n - 1:
            out.append(("buy", day(tr["buy"]), buy_msg(days, closes, tr, day)))
        if tr["ext"] and tr["ext"][-1] == n - 1:
            old = day(n)  # 원래 매도일(다음 세션)
            out.append(("ext", day(tr["sell"]), "\n".join([
                "\u21AA\uFE0F [VIX 30 돌파] 보유 연장",
                f"{fmt_day(days[-1])} VIX 종가 {closes[-1]:.2f} (전날 {closes[-2]:.2f}) → 보유 마지막 날 다시 {THRESHOLD:g} 돌파",
                f"{fmt_day(old)} 시가에 팔지 말고 계속 보유하세요.",
                f"새 매도일: {fmt_day(day(tr['sell']))} 미국장 시가 · {open_kst(day(tr['sell']))}",
            ])))
    return out


def reminders(days, closes, today_kst):
    """매수일·매도일 당일 아침 알림. 오늘 밤 열리는 미국 세션 = today_kst 날짜의 세션."""
    if not is_session(today_kst):
        return []
    prev = today_kst - dt.timedelta(days=1)
    while not is_session(prev):
        prev -= dt.timedelta(days=1)
    if days[-1] != prev:           # 어제 종가가 아직 없으면(데이터 지연) 판단 보류
        return []
    n = len(days)
    trades, act, day = active_trade(days, closes)
    if not act:
        return []
    out = []
    if day(act["buy"]) == today_kst:
        out.append(("buy", today_kst, "\n".join([
            "\u23F0 [VIX 30 돌파] 오늘 밤 매수",
            f"오늘 밤 미국장 시가에 {ETF} 매수 · {open_kst(today_kst)}",
            f"신호: {fmt_day(days[act['sig']])} VIX 종가 {closes[act['sig']]:.2f}",
            f"매도 예정: {fmt_day(day(act['sell']))} 시가 · {open_kst(day(act['sell']))}",
            "→ 토스에서 시장가 예약주문(매수)",
        ])))
    if day(act["sell"]) == today_kst:
        out.append(("sell", today_kst, "\n".join([
            "\U0001F514 [VIX 30 돌파] 오늘 매도",
            f"오늘 밤 미국장 시가에 {ETF} 매도 · {open_kst(today_kst)}",
            f"매수: {fmt_day(day(act['buy']))} 시가 (신호 {fmt_day(days[act['sig']])} VIX {closes[act['sig']]:.2f})",
            f"{fmt_day(days[-1])} VIX 종가 {closes[-1]:.2f}: 새 돌파 아님 → 연장 없음",
            "→ 토스에서 시장가 예약주문(매도)",
        ])))
    return out


def status(days, closes, src):
    trades, act, day = active_trade(days, closes)
    lines = ["\U0001F4CB [VIX 30 돌파] 현재 상태",
             f"마지막 VIX 종가: {fmt_day(days[-1])} {closes[-1]:.2f} (출처 {src})",
             f"규칙: VIX 종가 {THRESHOLD:g} 새로 돌파 → 다음 날 시가 {ETF} 매수 → {HOLD}거래일 뒤 시가 매도"]
    if act and act["buy"] == len(days):
        lines.append(f"매수 대기: {fmt_day(day(act['buy']))} 시가 매수 → {fmt_day(day(act['sell']))} 시가 매도 예정")
    elif act:
        lines.append(f"보유 중: {fmt_day(day(act['buy']))} 시가 매수 → {fmt_day(day(act['sell']))} 시가 매도 예정")
    else:
        lines.append(f"보유 없음. VIX가 {THRESHOLD:g} 아래에서 위로 종가 마감하는 날 알려드립니다.")
    recent = [t for t in trades if t["sig"] >= len(days) - 250][-3:]
    if recent:
        lines.append("최근 신호: " + ", ".join(f"{fmt_day(days[t['sig']])} {closes[t['sig']]:.1f}" for t in recent))
    return "\n".join(lines)


# ---------------- 발송 ----------------
def send(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("(TG_TOKEN/TG_CHAT_ID 없음 - 미리보기만)\n" + text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30) as r:
        resp = json.loads(r.read().decode())
    if not resp.get("ok"):
        raise SystemExit(f"텔레그램 발송 실패: {resp}")
    print("발송 완료:", text.splitlines()[0])


def main():
    now_ny = dt.datetime.now(NY)
    now_kst = dt.datetime.fromisoformat(os.environ["NOW_KST"]).replace(tzinfo=KST) if os.environ.get("NOW_KST") else dt.datetime.now(KST)
    days, closes, src = load_vix(now_ny)
    print(f"VIX {len(days)}일, 마지막 {days[-1]} {closes[-1]:.2f} ({src}) · 지금 한국시간 {now_kst:%m/%d %H:%M}")
    if (os.environ.get("MODE") or "run") == "status":
        send(status(days, closes, src))
        return
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    sent = set(state.get("sent", []))
    out = []
    last = days[-1].isoformat()
    if not state.get("last_processed") or state["last_processed"] < last:
        for kind, d, msg in new_close_events(days, closes):
            out.append(msg)
            if kind == "buy" and d == now_kst.date():
                sent.add(f"buy:{d}")          # 매수일 당일 아침에 받은 신호면 리마인더 생략
        state["last_processed"] = last
        state["last_vix"] = round(closes[-1], 2)
        state["source"] = src
    for kind, d, msg in reminders(days, closes, now_kst.date()):
        key = f"{kind}:{d}"
        if key not in sent:
            out.append(msg)
            sent.add(key)
    for m in out:
        send(m)
    if not out:
        print("오늘은 보낼 알림 없음")
    state["sent"] = sorted(sent)[-30:]
    if not os.environ.get("ASOF") or os.environ.get("WRITE_STATE"):
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
