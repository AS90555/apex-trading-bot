#!/usr/bin/env python3
"""
INV Scout — Invertiertes EMA-Crossover-Signal (Falsifikationstest)
===================================================================
Hypothese: EMA(20)×EMA(55) verliert in Trendrichtung → muss in Gegenrichtung gewinnen.

Signal:
  EMA(20) kreuzt ÜBER EMA(55) → SHORT (fade the bull trap)
  EMA(20) kreuzt UNTER EMA(55) → LONG  (fade the bear trap)

Exit-Matrix:
  V1: TP=1.0R / SL=1.0R  (SL = 1.5×ATR)
  V2: Time-Stop nach 18 Bars (3 Tage), Notfall-SL = −3R

Usage:
  venv/bin/python3 scripts/backtest/inv_scout.py
"""
import math
import os
import sys

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, aggregate_4h

TAKER_FEE  = 0.0006
SLIPPAGE   = 0.0005
COST_R     = (TAKER_FEE + SLIPPAGE) * 2

EMA_FAST   = 20
EMA_SLOW   = 55
ATR_PERIOD = 14
SL_ATR     = 1.5

V1_TP      = 1.0
V2_BARS    = 18
V2_EMERG   = 3.0

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = EMA_SLOW + ATR_PERIOD + 10


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _ema_series(values, period):
    out = [float("nan")] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i-1] * (1 - k)
    return out


def _atr_series(candles, period=ATR_PERIOD):
    out = [float("nan")] * len(candles)
    atr = None
    for i, c in enumerate(candles):
        tr = c["high"] - c["low"] if i == 0 else max(
            c["high"] - c["low"],
            abs(c["high"] - candles[i-1]["close"]),
            abs(c["low"]  - candles[i-1]["close"]),
        )
        atr = tr if atr is None else (atr * (period - 1) + tr) / period
        if i >= period - 1:
            out[i] = atr
    return out


# ─── KPIs ─────────────────────────────────────────────────────────────────────

def _p(t_abs, df):
    if df <= 0: return 1.0
    z = t_abs * (1 - 1/(4*df)) / math.sqrt(1 + t_abs**2/(2*df))
    return max(0.0, min(1.0, 2*(1 - 0.5*(1 + math.erf(z/math.sqrt(2))))))


def kpis(r_list):
    n = len(r_list)
    if n == 0:
        return dict(n=0, avg_r=0.0, wr=0.0, pf=0.0, avg_win=0.0, avg_loss=0.0, max_r=0.0, p=1.0)
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r <= 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if n > 1:
        var = sum((r - avg_r)**2 for r in r_list) / (n-1)
        std = math.sqrt(var) if var > 0 else 1e-9
        t   = avg_r / (std / math.sqrt(n))
    else:
        t = 0.0
    p = _p(abs(t), n-1)
    return dict(n=n, avg_r=round(avg_r,4), wr=round(wr,4), pf=round(pf,3),
                avg_win=round(avg_win,3), avg_loss=round(avg_loss,3),
                max_r=round(max(r_list),2), p=round(p,4))


def ascii_dist(r_list, bins=18):
    if not r_list: return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi: return f"    [{lo:.2f}R]\n"
    w = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r-lo)/w), bins-1)] += 1
    max_c = max(counts) or 1
    lines = [f"    {lo+i*w:+.2f}R │{'█'*int(c/max_c*24)} ({c})" for i, c in enumerate(counts)]
    return "\n".join(lines) + "\n"


# ─── Invertiertes Signal: bull-cross → SHORT, bear-cross → LONG ──────────────

def run_v1(candles, inv_direction):
    """inv_direction: 'short' = fade bull-cross | 'long' = fade bear-cross"""
    closes = [c["close"] for c in candles]
    e20    = _ema_series(closes, EMA_FAST)
    e55    = _ema_series(closes, EMA_SLOW)
    atrs   = _atr_series(candles)
    results  = []
    in_trade = False

    for i in range(WARMUP, len(candles)):
        if math.isnan(e20[i]) or math.isnan(e55[i]) or math.isnan(atrs[i]):
            continue

        if not in_trade:
            bull_x = e20[i-1] <= e55[i-1] and e20[i] > e55[i]
            bear_x = e20[i-1] >= e55[i-1] and e20[i] < e55[i]
            # Invertiert: bull_cross → SHORT, bear_cross → LONG
            signal = (inv_direction == "short" and bull_x) or \
                     (inv_direction == "long"  and bear_x)
            if signal:
                entry   = candles[i]["close"]
                sl_dist = atrs[i] * SL_ATR
                sl_pct  = sl_dist / entry
                if inv_direction == "short":
                    sl = entry + sl_dist
                    tp = entry - sl_dist * V1_TP
                else:
                    sl = entry - sl_dist
                    tp = entry + sl_dist * V1_TP
                in_trade = True
                continue
        else:
            c = candles[i]
            if inv_direction == "short":
                if c["high"] >= sl:
                    results.append(-1.0 - COST_R/sl_pct)
                    in_trade = False
                elif c["low"] <= tp:
                    results.append(+V1_TP - COST_R/sl_pct)
                    in_trade = False
            else:
                if c["low"] <= sl:
                    results.append(-1.0 - COST_R/sl_pct)
                    in_trade = False
                elif c["high"] >= tp:
                    results.append(+V1_TP - COST_R/sl_pct)
                    in_trade = False
    return results


def run_v2(candles, inv_direction):
    closes = [c["close"] for c in candles]
    e20    = _ema_series(closes, EMA_FAST)
    e55    = _ema_series(closes, EMA_SLOW)
    atrs   = _atr_series(candles)
    results  = []
    in_trade = False
    entry_bar = 0

    for i in range(WARMUP, len(candles)):
        if math.isnan(e20[i]) or math.isnan(e55[i]) or math.isnan(atrs[i]):
            continue

        if not in_trade:
            bull_x = e20[i-1] <= e55[i-1] and e20[i] > e55[i]
            bear_x = e20[i-1] >= e55[i-1] and e20[i] < e55[i]
            signal = (inv_direction == "short" and bull_x) or \
                     (inv_direction == "long"  and bear_x)
            if signal:
                entry     = candles[i]["close"]
                sl_dist   = atrs[i] * SL_ATR
                sl_pct    = sl_dist / entry
                emerg_sl  = entry + sl_dist * V2_EMERG if inv_direction == "short" \
                            else entry - sl_dist * V2_EMERG
                in_trade  = True
                entry_bar = i
                continue
        else:
            c   = candles[i]
            bar = i - entry_bar
            emerg = (inv_direction == "short" and c["high"] >= emerg_sl) or \
                    (inv_direction == "long"  and c["low"]  <= emerg_sl)
            if emerg:
                results.append(-V2_EMERG - COST_R/sl_pct)
                in_trade = False
                continue
            if bar >= V2_BARS:
                pnl_pct = (entry - c["close"]) / entry if inv_direction == "short" \
                          else (c["close"] - entry) / entry
                results.append(pnl_pct / sl_pct - COST_R/sl_pct)
                in_trade = False
    return results


# ─── Output ───────────────────────────────────────────────────────────────────

def row(label, k):
    print(f"  {label:<24} │  n={k['n']:>4}  AvgR={k['avg_r']:+.4f}R"
          f"  WR={k['wr']*100:.1f}%  PF={k['pf']:.2f}"
          f"  AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R  p={k['p']:.4f}")


def main():
    all_v1, all_v2 = [], []

    print("═" * 74)
    print("  INV SCOUT — Falsifikationstest: Invertiertes EMA(20)×EMA(55)-Signal")
    print("  Bull-Cross → SHORT  |  Bear-Cross → LONG")
    print("  V1: TP=1R/SL=1R  |  V2: Time-Stop 18×4H, Notfall-SL=−3R")
    print("═" * 74)

    for asset in ASSETS:
        candles  = aggregate_4h(load_csv(asset, "15m"))
        v1_short = run_v1(candles, "short")   # fade bull-cross
        v1_long  = run_v1(candles, "long")    # fade bear-cross
        v2_short = run_v2(candles, "short")
        v2_long  = run_v2(candles, "long")
        v1 = v1_short + v1_long
        v2 = v2_short + v2_long
        all_v1 += v1
        all_v2 += v2

        print(f"\n  {asset}")
        row("V1 fade bull-cross (SHORT)", kpis(v1_short))
        row("V1 fade bear-cross (LONG)",  kpis(v1_long))
        row("V2 fade bull-cross (SHORT)", kpis(v2_short))
        row("V2 fade bear-cross (LONG)",  kpis(v2_long))

    print("\n" + "═" * 74)
    print("  PORTFOLIO GESAMT")
    print("─" * 74)
    k1 = kpis(all_v1)
    k2 = kpis(all_v2)
    row("INV V1 (TP=1R/SL=1R)",     k1)
    row("INV V2 (18-Bar Time-Stop)", k2)

    print("\n  V1 R-Verteilung:")
    print(ascii_dist(all_v1))
    print("  V2 R-Verteilung:")
    print(ascii_dist(all_v2))

    print("═" * 74)
    print("  FALSIFIKATIONS-URTEIL")
    print("─" * 74)
    for label, k in [("INV V1", k1), ("INV V2", k2)]:
        if k["avg_r"] > 0 and k["p"] < 0.05 and k["n"] >= 50:
            verdict = "✅ GO — Inversion hat Edge!"
        elif k["avg_r"] > 0:
            verdict = f"⚠️  Positiv aber n.s. (p={k['p']:.4f})"
        else:
            verdict = f"❌ Auch Inversion verliert — kein direktionales Signal in EMA-Cross"
        print(f"  {label}: {verdict}")
    print("═" * 74)


if __name__ == "__main__":
    main()
