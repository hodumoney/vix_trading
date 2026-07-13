#!/usr/bin/env python3
"""
매일 실행되는 VIX/QLD 종가 수집 + 곡선 재적합 스크립트.

동작:
  1) 데이터 출처(Stooq 우선, Yahoo 폴백)에서 최근 종가를 받아온다.
  2) data.json에 아직 없는 '거래일'을 오래된 순으로 추가한다.
  3) 각 보유기간(1·2·3·5년) 대기버퍼에 하루를 넣고,
     버퍼가 가득 차면 가장 오래된 날의 연율화 성장을 확정해 충분통계량에 반영한다.
  4) 갱신된 data.json을 저장한다 (GitHub Actions가 커밋).

의존성 없음(표준 라이브러리만). Python 3.9+.
"""
import json, os, sys, urllib.request, urllib.error, io, csv, math, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")

# ---------- 데이터 출처 ----------
def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (vix-dca-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def from_stooq(symbol):
    """Stooq 일별 CSV. 예: ^vix, qld.us  ->  {date(iso): close}"""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    txt = _get(url)
    out = {}
    rdr = csv.DictReader(io.StringIO(txt))
    for row in rdr:
        d = row.get("Date"); c = row.get("Close")
        if not d or not c or c == "N/A":
            continue
        try:
            out[d] = float(c)
        except ValueError:
            pass
    return out

def from_yahoo(symbol):
    """Yahoo 비공식 차트 API 폴백. symbol 예: ^VIX, QLD"""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range=1mo&interval=1d")
    txt = _get(url)
    j = json.loads(txt)
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
        out[d] = float(c)
    return out

def fetch(symbol_stooq, symbol_yahoo):
    for fn, sym in ((from_stooq, symbol_stooq), (from_yahoo, symbol_yahoo)):
        try:
            m = fn(sym)
            if m:
                return m
        except Exception as e:
            print(f"  경고: {fn.__name__}({sym}) 실패 -> {e}", file=sys.stderr)
    return {}

# ---------- 충분통계량 증분 갱신 ----------
def fold(hz, v, G):
    L = math.log(v)
    s, t = hz["s"], hz["t"]
    s[0] += 1; s[1] += L; s[2] += L*L; s[3] += L**3; s[4] += L**4
    t[0] += G; t[1] += G*L; t[2] += G*L*L
    hz["n"] += 1

def append_day(data, v, p):
    """모든 기간 버퍼에 하루(v,p)를 넣고, 가득 찬 기간은 성장 확정."""
    for hz in data["horizons"]:
        h = hz["h"]
        hz["buf"].append({"v": round(v, 4), "p": round(p, 6)})
        if len(hz["buf"]) > h:
            old = hz["buf"].pop(0)
            if old["p"] > 0 and p > 0 and old["v"] > 0:
                G = (p / old["p"]) ** (252.0 / h)   # 연율화 성장
                fold(hz, old["v"], G)

# ---------- 메인 ----------
def main():
    data = json.load(open(DATA))
    last = data["lastDate"]

    vix = fetch("^vix", "^VIX")
    qld = fetch("qld.us", "QLD")
    if not vix or not qld:
        print("데이터를 가져오지 못했습니다. 종료.", file=sys.stderr)
        sys.exit(1)

    # 두 소스에 공통으로 존재하고, 마지막 저장일보다 이후인 거래일만
    common = sorted(set(vix) & set(qld))
    new_days = [d for d in common if d > last]
    if not new_days:
        print(f"추가할 새 거래일 없음 (마지막 {last}).")
        return

    added = 0
    for d in new_days:
        append_day(data, float(vix[d]), float(qld[d]))
        data["lastDate"] = d
        added += 1
        print(f"  + {d}  VIX={vix[d]:.2f}  QLD={qld[d]:.2f}")

    json.dump(data, open(DATA, "w"))
    print(f"완료: {added}일 추가, 마지막 {data['lastDate']}.")

if __name__ == "__main__":
    main()
