#!/usr/bin/env python3
"""
빅스 매수법 점검 보고 (텔레그램)

매일 한국시간 아침 8:00에 실행됩니다.
  1) 새 거래일의 QQQ·VIX 종가를 history/qqq_vix.csv 에 덧붙입니다(2000년부터 쌓인 기록).
  2) 실전 거래(2026-11-20 신호 이후)가 하나 끝났으면 전체 백테스트를 다시 돌려 보고서를 보냅니다.
     - 누적 성과(TQQQ·QLD, 쉬는 돈 단기채)
     - 실전 거래 기록(규칙대로 했다면)
     - 기준값 주변 표: 돌파 VIX / 연장 중 매도 VIX / 약세장 기준 / 기한을 하나씩 바꿨을 때 연 수익
     - 판정: 미리 정한 조건을 넘는 값이 있으면 "검토 필요", 아니면 "유지" (자동으로 바꾸지 않음)
수동 실행(MODE=report)하면 거래가 없어도 바로 보고서를 보냅니다.

TQQQ·QLD는 QQQ로 합성합니다: r = L·r_QQQ − (L−1)·금리/252 − k/252 (연구 노트북과 같은 방식)
표준 라이브러리만 사용. Python 3.9+
"""
import csv, json, os, sys, datetime as dt
from zoneinfo import ZoneInfo
import signal30 as S

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history", "qqq_vix.csv")
STATE = os.path.join(HERE, "report_state.json")
NY, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")

RULE = {"X": 30.0, "L": 25.0, "B": -0.20, "cap": 15}          # 지금 쓰는 기준값 (signal30.py와 같아야 함)
GRID = {"X": [28.0, 29.0, 30.0, 31.0, 32.0],
        "L": [21.0, 23.0, 25.0, 27.0, 29.0],
        "B": [-0.10, -0.15, -0.20, -0.25, -0.30],
        "cap": [5, 10, 15, 20, 25]}
NAME = {"X": "돌파 VIX", "L": "연장 중 매도 VIX", "B": "약세장 기준(QQQ 고점 대비)", "cap": "연장 기한"}
FEE = 0.0015
LIVE_SINCE = dt.date(2026, 11, 20)                              # 알림으로 실전 시작한 첫 신호
SPLIT = dt.date(2013, 1, 1)                                     # 앞·뒤 기간 나누는 날
ETFS = {"TQQQ": (3, -0.010392764831802336), "QLD": (2, -0.004641003744702882)}
# 연방기금금리 연평균(%) — 합성 ETF 비용과 쉬는 돈(단기채) 수익에 씀. 없는 해는 마지막 값 사용. 해마다 고쳐 주세요.
RF_YEAR = {1999: 4.97, 2000: 6.24, 2001: 3.89, 2002: 1.67, 2003: 1.13, 2004: 1.35, 2005: 3.21, 2006: 4.96,
           2007: 5.02, 2008: 1.93, 2009: 0.16, 2010: 0.18, 2011: 0.10, 2012: 0.14, 2013: 0.11, 2014: 0.09,
           2015: 0.13, 2016: 0.39, 2017: 1.00, 2018: 1.83, 2019: 2.16, 2020: 0.38, 2021: 0.08, 2022: 1.68,
           2023: 5.02, 2024: 5.14, 2025: 4.21, 2026: 3.60}


# ---------------- 기록 ----------------
def load_hist():
    d, qo, qc, v = [], [], [], []
    with open(HIST, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d.append(dt.date.fromisoformat(row["DATE"]))
            qo.append(float(row["QQQ_O"])); qc.append(float(row["QQQ_C"])); v.append(float(row["VIX"]))
    return d, qo, qc, v


def append_new(d, qo, qc, v, now_ny):
    """야후 QQQ·VIX로 새 거래일 추가. QQQ는 마지막 저장값에 수익률을 이어 붙임(가격 수준 차이 무시)."""
    try:
        yq = S._yahoo("QQQ", "3mo"); yv = S._yahoo("^VIX", "3mo")
    except Exception as e:
        print("야후 실패:", e, file=sys.stderr)
        return 0
    days = [t for t in S._trim(yq, now_ny) if t > d[-1]]
    if d[-1] not in yq:
        print(f"야후 QQQ에 {d[-1]} 값이 없어 이어 붙이지 못함", file=sys.stderr)
        return 0
    prev_y, prev_s, added = yq[d[-1]][1], qc[-1], []
    for t in days:
        o, c = yq[t]
        if o is None or t not in yv:
            break
        added.append((t, prev_s * o / prev_y, prev_s * c / prev_y, yv[t][1]))
        prev_y, prev_s = c, prev_s * c / prev_y
    for t, o, c, x in added:
        d.append(t); qo.append(o); qc.append(c); v.append(x)
    if added:
        with open(HIST, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for t, o, c, x in added:
                w.writerow([t.isoformat(), f"{o:.4f}", f"{c:.4f}", f"{x:.2f}"])
    return len(added)


# ---------------- 백테스트 ----------------
class Data:
    def __init__(self, d, qo, qc, v):
        self.d, self.qo, self.qc, self.v, self.n = d, qo, qc, v, len(d)
        last = max(RF_YEAR)
        self.rf = [RF_YEAR.get(t.year, RF_YEAR[last]) / 100 / 252 for t in d]
        self.hi = []
        win = []
        for i, c in enumerate(qc):            # 52주(252거래일) 최고 종가, 최소 60일
            win.append(c)
            if len(win) > 252:
                win.pop(0)
            self.hi.append(max(win) if len(win) >= 60 else float("nan"))
        self.etf = {name: self._synth(L, k) for name, (L, k) in ETFS.items()}

    def _synth(self, L, k):
        O, C = [100 * self.qo[0] / self.qc[0]], [100.0]
        for i in range(1, self.n):
            rq = self.qc[i] / self.qc[i - 1] - 1
            gap = self.qo[i] / self.qc[i - 1] - 1
            r = L * rq - (L - 1) * self.rf[i] - k / 252
            O.append(C[-1] * (1 + L * gap)); C.append(C[-1] * (1 + r))
        R = [O[i + 1] / O[i] - 1 for i in range(self.n - 1)] + [0.0]
        return O, C, R


def backtest(D, etf="TQQQ", X=30.0, L=25.0, B=-0.20, cap=15):
    O, Cl, R = D.etf[etf]
    V, n = D.v, D.n
    tr, i = [], 1
    while i < n - 1:
        if V[i] >= X and V[i - 1] < X:
            a = i + 1
            if tr and tr[-1][2] == a:
                tr[-1][2] = a + 2
            else:
                tr.append([i, a, a + 2])
            i = a + 1
        else:
            i += 1
    w, trades = [0.0] * n, []
    for s, a, e in tr:
        if e >= n:
            if a < n:
                trades.append(dict(s=s, a=a, e=e, x=None, ext=None, done=False))
                for k in range(a, n):
                    w[k] = 1.0
            continue
        x, last, ext = e, e - 1, False
        if Cl[last] / O[a] * (1 - FEE) - 1 < 0 and V[last] < V[s] and D.qc[last] / D.hi[last] - 1 > B:
            ext, x, why = True, None, None
            for t in range(e, min(e + cap, n)):
                if V[t] < L:
                    x, why = t + 1, "calm"
                    break
            if x is None:
                x, why = e + cap, "deadline"
        else:
            why = "profit" if Cl[last] / O[a] * (1 - FEE) - 1 >= 0 else "loss"
        for k in range(a, min(x, n)):
            w[k] = 1.0
        trades.append(dict(s=s, a=a, e=e, x=x, ext=ext, why=why, done=x <= n - 1))
    eq, peak, mdd, prev = 1.0, 1.0, 0.0, 0.0
    g = {"h1": 1.0, "h2": 1.0}
    for t in range(n):
        r = w[t] * R[t] + (1 - w[t]) * D.rf[t] - FEE * abs(w[t] - prev)
        prev = w[t]
        eq *= 1 + r; peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        g["h1" if D.d[t] < SPLIT else "h2"] *= 1 + r
    yrs = lambda a, b: (b - a).days / 365.25
    i2 = next(k for k in range(n) if D.d[k] >= SPLIT)
    out = dict(cagr=eq ** (1 / yrs(D.d[0], D.d[-1])) - 1, mdd=mdd,
               h1=g["h1"] ** (1 / yrs(D.d[0], D.d[i2 - 1])) - 1, h2=g["h2"] ** (1 / yrs(D.d[i2], D.d[-1])) - 1)
    for t in trades:
        a = t["a"]
        if t["done"]:
            t["ret"] = O[t["x"]] / O[a] * (1 - FEE) ** 2 - 1
        else:
            t["ret"] = Cl[n - 1] / O[a] * (1 - FEE) - 1     # 아직 보유: 마지막 종가 평가
    closed = [t["ret"] for t in trades if t["done"]]
    out.update(trades=trades, n=len(closed), win=sum(r >= 0 for r in closed) / len(closed), avg=sum(closed) / len(closed))
    return out


# ---------------- 보고서 ----------------
def pct(x, sign=False):
    s = f"{x * 100:+.1f}%" if sign else f"{x * 100:.1f}%"
    return s.replace("-", "−")


SHORT = {"X": "돌파 기준", "L": "연장 중 매도", "B": "약세장 기준", "cap": "연장 기한"}


def label(p, val):
    if p in ("X", "L"):
        return f"VIX {val:g}"
    if p == "B":
        return pct(val).replace(".0%", "%")
    return f"D+{3 + val}"


def sensitivity(D):
    cur = backtest(D, **RULE)
    res = {}
    for p, vals in GRID.items():
        for val in vals:
            res[(p, val)] = cur if val == RULE[p] else backtest(D, **{**RULE, p: val})
    return cur, res


def judge(p, cur, res):
    """(바꿀지 검토 대상 값 또는 None, 한 줄 이유)."""
    vals = GRID[p]
    i0 = vals.index(RULE[p])
    nbs = lambda j: [vals[k] for k in (j - 1, j + 1) if 0 <= k < len(vals) and k != i0]
    checks = []
    for j, val in enumerate(vals):
        if j == i0:
            continue
        r = res[(p, val)]
        fail = None
        if r["cagr"] < cur["cagr"] + 0.01:
            fail = "small"
        elif not (r["h1"] > cur["h1"] and r["h2"] > cur["h2"]):
            fail = "half"
        elif r["mdd"] < cur["mdd"] - 0.03:
            fail = "mdd"
        else:
            bad = [v2 for v2 in nbs(j) if res[(p, v2)]["cagr"] <= cur["cagr"]]
            fail = ("nb", bad[0]) if bad else None
        checks.append((val, r, fail))
    for val, r, fail in checks:
        if fail is None:
            return val, f"{label(p, val)} → 연 {pct(r['cagr'])} (지금 {pct(cur['cagr'])}), 앞·뒤 기간 모두 더 좋음"
    val, r, fail = max(checks, key=lambda c: c[1]["cagr"])
    if r["cagr"] <= cur["cagr"]:
        return None, "지금 값이 가장 좋음"
    head = f"{label(p, val)} → 연 {pct(r['cagr'])}, 더 높지만"
    if fail == "small":
        return None, f"{head} 차이 {(r['cagr'] - cur['cagr']) * 100:.1f}%p로 작음"
    if fail == "half":
        per = "2000–12년" if r["h1"] <= cur["h1"] else "2013년 이후"
        return None, f"{head} {per}엔 나아지지 않음"
    if fail == "mdd":
        return None, f"{head} 최대낙폭이 {pct(r['mdd'])}로 깊음"
    nb = fail[1]
    return None, f"{head} 옆 값({label(p, nb)}) {pct(res[(p, nb)]['cagr'])}라 한 점만 좋음"


WHY_KR = {"profit": "이익", "loss": "손실", "calm": "VIX 25 미만", "deadline": "기한"}


def report(D, title_line):
    cur, res = sensitivity(D)
    lines = [f"📊 {S.TAG} 점검 보고", title_line, "", f"▶ 실전 기록 ({LIVE_SINCE.month}/{LIVE_SINCE.day} 신호부터)"]
    live = [t for t in cur["trades"] if D.d[t["s"]] >= LIVE_SINCE]
    tot, n_done = 1.0, 0
    for t in live:
        sig = S.fmt_day(D.d[t["s"]])
        if t["done"]:
            lines.append(f"· {sig} 신호 → {S.fmt_day(D.d[t['x']])} 매도: {pct(t['ret'], True)} ({WHY_KR.get(t.get('why'), '')})")
            tot *= 1 + t["ret"]; n_done += 1
        else:
            lines.append(f"· {sig} 신호 · 보유 중: {pct(t['ret'], True)} ({S.fmt_day(D.d[-1])} 종가)")
    if not live:
        lines.append("· 아직 없음")
    if n_done >= 2:
        lines.append(f"끝난 거래 {n_done}건 누적 {pct(tot - 1, True)}")
    lines += ["", f"▶ 백테스트 {D.d[0].year}~{D.d[-1]:%Y.%m.%d} (TQQQ)",
              f"연 {pct(cur['cagr'])} · 최대낙폭 {pct(cur['mdd'])} · 승률 {cur['win'] * 100:.0f}%", ""]
    judged = [(p, *judge(p, cur, res)) for p in GRID]
    n_flag = sum(1 for _, v, _ in judged if v is not None)
    lines.append("▶ 기준값: 모두 유지 ✅" if not n_flag else f"▶ 기준값: {n_flag}개 검토 필요 ⚠️")
    for p, val, why in judged:
        lines.append(f"{'⚠️' if val is not None else '✅'} {SHORT[p]} {label(p, RULE[p])}")
        lines.append(f"   {why}")
    if n_flag:
        lines.append("→ 자동으로 바꾸지 않습니다. 바꿀지 직접 정하세요.")
    return "\n".join(lines)


def main():
    now_ny = dt.datetime.now(NY)
    d, qo, qc, v = load_hist()
    added = 0 if os.environ.get("OFFLINE") else append_new(d, qo, qc, v, now_ny)
    print(f"기록 {len(d)}일, 마지막 {d[-1]} (새로 추가 {added}일)")
    D = Data(d, qo, qc, v)
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    done = [t for t in backtest(D, **RULE)["trades"] if t["done"] and D.d[t["s"]] >= LIVE_SINCE]
    new = [t for t in done if D.d[t["s"]].isoformat() > state.get("last_reported_sig", "")]
    force = (os.environ.get("MODE") or "run") == "report"
    if new or force:
        if new:
            t = new[-1]
            title = f"거래 종료: {S.fmt_day(D.d[t['s']])} 신호 → {S.fmt_day(D.d[t['x']])} 매도"
            state["last_reported_sig"] = D.d[t["s"]].isoformat()
        else:
            title = "수동 점검"
        S.send(report(D, title))
    else:
        print("끝난 실전 거래 없음 → 보고 없음")
    state["last_data"] = d[-1].isoformat()
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
