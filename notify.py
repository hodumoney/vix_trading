#!/usr/bin/env python3
"""
매 거래일 밤 11시(한국시간)에 실행되는 매수 신호 텔레그램 알림.

- 매수금액: data.json의 곡선 + '전일 종가 VIX'로 계산 (곡선과 일치).
- 참고 표시: 현재 장중 VIX, QLD 현재가, 환율.
- 미국 휴장일이면 매수금액 대신 VIX만 안내.
- 발송: Telegram Bot API.

환경변수(GitHub Secrets):
  TG_TOKEN    BotFather가 준 봇 토큰
  TG_CHAT_ID  내 chat id (@userinfobot으로 확인)
표준 라이브러리만 사용.
"""
import json, os, sys, math, io, csv, urllib.request, urllib.parse, datetime, zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")

# ---------- 곡선 로드 & 매수함수 ----------
def load_curve():
    d = json.load(open(DATA))
    XtX = [[0,0,0],[0,0,0],[0,0,0]]; Xty = [0,0,0]
    for hz in d["horizons"]:
        w = 1.0/hz["n"]; s = hz["s"]; t = hz["t"]
        M = [[s[0],s[1],s[2]],[s[1],s[2],s[3]],[s[2],s[3],s[4]]]
        for i in range(3):
            for j in range(3): XtX[i][j] += w*M[i][j]
            Xty[i] += w*t[i]
    a,b,c = solve3(XtX, Xty)
    vlo = math.exp(-b/(2*c)); mulo = a+b*math.log(vlo)+c*math.log(vlo)**2
    slope = (d["maxA"]-d["minA"])/((a+b*math.log(d["vhi"])+c*math.log(d["vhi"])**2)-mulo)
    prev_vix = d["horizons"][0]["buf"][-1]["v"]   # 마지막 수집 = 전일 종가 VIX
    return d, (a,b,c,mulo,slope,vlo), prev_vix

def solve3(M, y):
    a = [row[:]+[y[i]] for i,row in enumerate(M)]
    for i in range(3):
        mx = i
        for k in range(i+1,3):
            if abs(a[k][i])>abs(a[mx][i]): mx = k
        a[i],a[mx] = a[mx],a[i]
        piv = a[i][i]
        for j in range(i,4): a[i][j] /= piv
        for k in range(3):
            if k==i: continue
            f = a[k][i]
            for j in range(i,4): a[k][j] -= f*a[i][j]
    return a[0][3],a[1][3],a[2][3]

def amount(coef, minA, maxA, v):
    a,b,c,mulo,slope,_ = coef
    l = math.log(v); mu = a+b*l+c*l*l
    return int(round(min(max(minA+slope*(mu-mulo), minA), maxA)/100)*100)

def mood(v):
    if v < 14: return "아주 잔잔"
    if v < 20: return "평온"
    if v < 27: return "조정"
    if v < 40: return "불안"
    return "공포·패닉"

# ---------- 실시간 참고값 ----------
def _get(url, t=25):
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return r.read().decode("utf-8","replace")

def live_close(symbol_stooq, symbol_yahoo):
    """(현재가, 오늘거래여부) 반환."""
    today = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    try:
        j = json.loads(_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_yahoo}?range=1d&interval=1d"))
        res = j["chart"]["result"][0]; meta = res["meta"]
        price = meta.get("regularMarketPrice"); ts = meta.get("regularMarketTime")
        d = None
        if ts:
            d = datetime.datetime.utcfromtimestamp(ts).astimezone(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if price: return float(price), (d == today)
    except Exception as e:
        print("live yahoo 실패:", e, file=sys.stderr)
    try:
        txt = _get(f"https://stooq.com/q/l/?s={symbol_stooq}&f=sd2t2c&h&e=csv")
        row = list(csv.DictReader(io.StringIO(txt)))[0]
        return float(row["Close"]), (row.get("Date")==today)
    except Exception as e:
        print("live stooq 실패:", e, file=sys.stderr)
    return None, None

def fx_usdkrw():
    try:
        j = json.loads(_get("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=1d&interval=1d"))
        return float(j["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None

# ---------- 메시지 구성 ----------
def build_message():
    d, coef, prev_vix = load_curve()
    amt = amount(coef, d["minA"], d["maxA"], prev_vix)
    cur_vix, vix_open = live_close("^vix", "^VIX")
    qld, qld_open = live_close("qld.us", "QLD")
    fx = fx_usdkrw()

    ny = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    if qld is None and cur_vix is None:
        is_open = ny.weekday() < 5     # 조회 실패 시 평일이면 개장 추정
    else:
        is_open = bool(qld_open if qld is not None else vix_open)

    kst = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Seoul")).strftime("%m/%d")
    lines = [f"\U0001F4C8 [VIX 적립] {kst} 신호"]
    if is_open:
        lines.append(f"매수 기준: 전일 종가 VIX {prev_vix:.1f} · {mood(prev_vix)}")
        shares = ""
        if qld and fx:
            usd = amt/fx
            shares = f" (약 {usd/qld:.2f}주 @ ${qld:.1f}, 환율 {fx:,.0f}원)"
        lines.append(f"오늘 매수: {amt:,}원{shares}")
        if cur_vix is not None:
            lines.append(f"참고: 현재 장중 VIX {cur_vix:.1f}")
        lines.append("\u2192 토스에서 QLD를 위 금액만큼 매수")
    else:
        show = cur_vix if cur_vix is not None else prev_vix
        lines.append("오늘은 미국 증시 휴장(또는 주말)입니다.")
        lines.append(f"참고 VIX: {show:.1f} · {mood(show)}")
        lines.append("매수는 다음 거래일에.")
    return "\n".join(lines)

def send(text):
    token = os.environ["TG_TOKEN"]; chat = os.environ["TG_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=25) as r:
        resp = json.loads(r.read().decode())
    if not resp.get("ok"):
        print("텔레그램 발송 실패:", resp, file=sys.stderr); sys.exit(1)
    print("발송 완료 (message_id:", resp["result"]["message_id"], ")")

if __name__ == "__main__":
    body = build_message()
    print(body)
    if os.environ.get("TG_TOKEN"):
        send(body)
    else:
        print("(TG_TOKEN 없음 - 미리보기만 출력)")
