#!/usr/bin/env python3
"""
KDT Phase 2 — Filter-Attribution.

Baseline: EMA=50, Entry-Window=2, TP=3R, SHORT-only.
Jeder Filter wird isoliert getestet. Delta = MIT - OHNE Filter.

Filter-Liste:
  F-01 ATR-Expansion    : ATR(14) > ATR_SMA(20) × k  (Volatilität wächst)
  F-02 Vol-Confirmation : Breakout-Kerze Vol > Vol_SMA(20) × k
  F-03 Body-Minimum     : Body[-0] > ATR(14) × k  (kein Micro-Doji)
  F-04 Distance-Check   : SL-Distanz (Risk) < ATR(14) × k  (enger SL = starkes Signal)
  F-05 EMA-Slope        : EMA(50) steigend (Slope > 0 der letzten N Kerzen)

Verwendung:
  python3 scripts/backtest/kdt_phase2.py
"""
import itertools
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE
from scripts.backtest.squeeze_scout  import aggregate_1h
from scripts.backtest.kdt_scout     import _ema_series

# ─── Gewinner-Parameter aus Phase 1 ──────────────────────────────────────────
EMA_PERIOD   = 50
ENTRY_WINDOW = 2
TP_R         = 3.0
DIRECTION    = "short"

IS_START = "2025-04-21"
IS_END   = "2026-02-10"

ASSETS = ["NEAR", "ETH", "DOGE", "AAVE", "TIA", "INJ", "APT"]

# Bonferroni: 27 Grid + 5 Filter × 3 Varianten = 27+15 = 42 Tests
N_TESTS          = 42
ALPHA_BONFERRONI = 0.05 / N_TESTS   # 0.00119

# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _atr(candles: list[dict], i: int, period: int = 14) -> float:
    if i < period:
        return 0.0
    trs = []
    for j in range(i - period + 1, i + 1):
        h, l, pc = candles[j]["high"], candles[j]["low"], candles[j-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


def _sma_list(values: list[float], i: int, period: int) -> float:
    if i < period:
        return 0.0
    return sum(values[i - period:i]) / period


def build_full_indicators(candles: list[dict]) -> list[dict]:
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    ema50   = _ema_series(closes, EMA_PERIOD)

    # ATR-Serie einmal berechnen (O(n) statt O(n²))
    atr_series = [0.0] * len(candles)
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i < 14:
            atr_series[i] = tr
        elif i == 14:
            atr_series[i] = sum(max(candles[j]["high"]-candles[j]["low"],
                                    abs(candles[j]["high"]-candles[j-1]["close"]),
                                    abs(candles[j]["low"]-candles[j-1]["close"]))
                                for j in range(1, 15)) / 14
        else:
            atr_series[i] = (atr_series[i-1] * 13 + tr) / 14

    result = []
    for i in range(len(candles)):
        atr14     = atr_series[i]
        atr_sma   = _sma_list(atr_series, i, 20)
        vol_sma   = _sma_list(volumes, i, 20)
        body      = abs(candles[i]["close"] - candles[i]["open"])
        ema_slope = (ema50[i] - ema50[i-3]) if i >= 3 and ema50[i-3] > 0 else 0
        result.append({
            "ema50":     ema50[i],
            "atr14":     atr14,
            "atr_sma20": atr_sma,
            "vol_sma20": vol_sma,
            "body":      body,
            "ema_slope": ema_slope,
        })
    return result


# ─── Filter-Definitionen ──────────────────────────────────────────────────────

def make_filters(param: float) -> dict:
    return {
        "F-01-ATR-Expand":  lambda c0, c1, c2, ind, i: (
            ind[i]["atr_sma20"] > 0 and
            ind[i]["atr14"] > param * ind[i]["atr_sma20"]),
        "F-02-Vol-Confirm": lambda c0, c1, c2, ind, i: (
            ind[i]["vol_sma20"] > 0 and
            c0["volume"] > param * ind[i]["vol_sma20"]),
        "F-03-Body-Min":    lambda c0, c1, c2, ind, i: (
            ind[i]["atr14"] > 0 and
            ind[i]["body"] > param * ind[i]["atr14"]),
        "F-04-Tight-SL":    lambda c0, c1, c2, ind, i: (
            ind[i]["atr14"] > 0 and
            (c0["high"] - c0["low"]) < param * ind[i]["atr14"]),
        "F-05-EMA-Slope":   lambda c0, c1, c2, ind, i: (
            ind[i]["ema_slope"] > 0),   # param nicht genutzt, immer True
    }


FILTER_PARAMS = {
    "F-01-ATR-Expand":  [1.0, 1.2, 1.5],
    "F-02-Vol-Confirm": [1.0, 1.5, 2.0],
    "F-03-Body-Min":    [0.1, 0.2, 0.3],
    "F-04-Tight-SL":    [1.0, 1.5, 2.0],
    "F-05-EMA-Slope":   [1.0],           # kein Parameter
}


# ─── Backtest mit optionalem Filter ──────────────────────────────────────────

def run_kdt_filtered(candles_1h: list[dict], inds: list[dict],
                     start: str, end: str,
                     filter_fn=None) -> list[float]:
    closes  = [c["close"] for c in candles_1h]
    ema50   = _ema_series(closes, EMA_PERIOD)
    warmup  = EMA_PERIOD + 20

    pending  = []
    in_trade = False
    trade    = {}
    results  = []

    for i, c in enumerate(candles_1h):
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")

        if in_trade:
            ae, sl, tp, risk = trade["ae"], trade["sl"], trade["tp"], trade["risk"]
            hit_sl = c["high"] >= sl
            hit_tp = c["low"]  <= tp
            if hit_sl and not hit_tp:
                results.append(-1.0 - (2 * ae * TAKER_FEE) / risk)
                in_trade = False; continue
            if hit_tp:
                results.append(TP_R - (2 * ae * TAKER_FEE) / risk)
                in_trade = False; continue
            continue

        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"]:
                    continue
                if day < start or day > end:
                    continue
                if c["low"] <= p["stop"]:
                    ae   = p["stop"] * (1 - SLIPPAGE)
                    risk = p["sl"] - ae
                    if risk > 0 and 0.001 <= risk / ae <= 0.20:
                        tp_p = ae - TP_R * risk
                        in_trade = True
                        trade = {"ae": ae, "sl": p["sl"], "tp": tp_p, "risk": risk}
                        triggered.append(p); break
                    triggered.append(p)
            pending = [p for p in pending
                       if p not in triggered and i <= p["expiry"]]

        if in_trade: continue
        if day < start or day > end or i < warmup or i < 2: continue

        c0, c1, c2 = candles_1h[i], candles_1h[i-1], candles_1h[i-2]
        e = ema50[i]
        if e <= 0: continue

        body0 = abs(c0["close"] - c0["open"])
        body1 = abs(c1["close"] - c1["open"])
        body2 = abs(c2["close"] - c2["open"])
        if body0 <= 0: continue

        vol0, vol1, vol2 = c0["volume"], c1["volume"], c2["volume"]
        if vol0 <= 0: continue

        if not (c0["close"] > c0["open"] and c1["close"] > c1["open"] and
                c2["close"] > c2["open"] and
                body0 < body1 < body2 and vol0 < vol1 < vol2 and
                c0["close"] > e):
            continue

        sl_p = c0["high"]
        stop = c0["low"]
        risk = sl_p - stop
        if not (0 < risk / stop <= 0.15):
            continue

        # Filter anwenden
        if filter_fn is not None and not filter_fn(c0, c1, c2, inds, i):
            continue

        pending.append({"stop": stop, "sl": sl_p, "expiry": i + ENTRY_WINDOW})

    return results


def kpis(r_list):
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "pf": 0.0, "p": 1.0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r-mean)**2 for r in r_list)/(n-1)) if n > 1 else 0
    t     = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1/(1+0.3275911*abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p*math.exp(-x*x)
    p = erfc(abs(t)/math.sqrt(2)) if t != 0 else 1.0
    return {"n": n, "avg_r": round(mean, 3), "wr": round(len(wins)/n, 3),
            "pf": round(gw/gl, 2) if gl > 0 else float("inf"), "p": round(p, 4)}


# ─── Haupt-Runner ─────────────────────────────────────────────────────────────

def main():
    print(f"\n{'═'*82}")
    print(f"  KDT Phase 2 — Filter-Attribution")
    print(f"  Baseline: EMA={EMA_PERIOD} Win={ENTRY_WINDOW} TP={TP_R}R  "
          f"IS: {IS_START}→{IS_END}")
    print(f"  Bonferroni α = 0.05/{N_TESTS} = {ALPHA_BONFERRONI:.5f}")
    print(f"{'═'*82}\n")

    # Daten laden + Indikatoren bauen
    all_candles = {}
    all_inds    = {}
    for asset in ASSETS:
        raw = load_csv(asset, "15m")
        if raw:
            candles = aggregate_1h(raw)
            all_candles[asset] = candles
            all_inds[asset]    = build_full_indicators(candles)

    # Baseline
    baseline_r = []
    for asset in ASSETS:
        if asset not in all_candles: continue
        baseline_r.extend(run_kdt_filtered(
            all_candles[asset], all_inds[asset], IS_START, IS_END))
    base_k = kpis(baseline_r)
    print(f"  BASELINE: n={base_k['n']}  AvgR={base_k['avg_r']:+.3f}  "
          f"WR={base_k['wr']*100:.1f}%  PF={base_k['pf']:.2f}  p={base_k['p']:.4f}\n")

    # Filter-Tests
    print(f"  {'Filter':<22} {'Param':>6} │ {'n':>4} {'AvgR':>7} {'Delta':>7} "
          f"{'WR':>6} {'PF':>5} {'p':>7} │ {'n-Verlust':>9} │ {'Verdikt'}")
    print(f"  {'─'*82}")

    keep_filters = []

    for fname, params in FILTER_PARAMS.items():
        best_delta = -999
        best_result = None
        for param in params:
            filters = make_filters(param)
            fn = filters[fname]
            r = []
            for asset in ASSETS:
                if asset not in all_candles: continue
                r.extend(run_kdt_filtered(
                    all_candles[asset], all_inds[asset], IS_START, IS_END, fn))
            k = kpis(r)
            delta = k["avg_r"] - base_k["avg_r"]
            n_loss_pct = (base_k["n"] - k["n"]) / base_k["n"] * 100 if base_k["n"] > 0 else 0
            bonf_ok = "✅" if k["p"] < ALPHA_BONFERRONI else ("🟡" if k["p"] < 0.05 else "  ")
            verdict = ""
            if delta >= 0.02 and n_loss_pct <= 40 and k["avg_r"] > 0:
                verdict = "KEEP 🟢"
            elif delta < -0.02:
                verdict = "SKIP ❌"
            else:
                verdict = "NEUTRAL"

            print(f"  {fname:<22} {param:>6.1f} │ {k['n']:>4} {k['avg_r']:>+7.3f} "
                  f"{delta:>+7.3f} {k['wr']*100:>5.1f}% {k['pf']:>5.2f} "
                  f"{k['p']:>7.4f} │ {n_loss_pct:>8.1f}% │ {bonf_ok} {verdict}")

            if delta > best_delta:
                best_delta = delta
                best_result = {"filter": fname, "param": param, "delta": delta,
                               "k": k, "n_loss_pct": n_loss_pct}

        if best_result and best_result["delta"] >= 0.02 and \
                best_result["n_loss_pct"] <= 40 and best_result["k"]["avg_r"] > 0:
            keep_filters.append(best_result)
        print()

    # Zusammenfassung
    print(f"{'═'*82}")
    print(f"  FILTER-ZUSAMMENFASSUNG\n")
    print(f"  Baseline: AvgR={base_k['avg_r']:+.3f}  n={base_k['n']}\n")

    if keep_filters:
        print(f"  KEEP-Filter ({len(keep_filters)}):")
        for f in sorted(keep_filters, key=lambda x: -x["delta"]):
            print(f"    {f['filter']:<22} param={f['param']:.1f}  "
                  f"Delta={f['delta']:+.3f}R  n={f['k']['n']}  "
                  f"n-Verlust={f['n_loss_pct']:.1f}%")

        # Kombinierter Test (Top-2)
        if len(keep_filters) >= 2:
            f1, f2 = keep_filters[0], keep_filters[1]
            print(f"\n  Kombinations-Check ({f1['filter']} + {f2['filter']}):")
            fn1 = make_filters(f1["param"])[f1["filter"]]
            fn2 = make_filters(f2["param"])[f2["filter"]]
            combo_fn = lambda c0,c1,c2,ind,i: fn1(c0,c1,c2,ind,i) and fn2(c0,c1,c2,ind,i)
            combo_r = []
            for asset in ASSETS:
                if asset not in all_candles: continue
                combo_r.extend(run_kdt_filtered(
                    all_candles[asset], all_inds[asset], IS_START, IS_END, combo_fn))
            combo_k = kpis(combo_r)
            combo_delta = combo_k["avg_r"] - base_k["avg_r"]
            redundant = abs(combo_delta - f1["delta"]) < 0.02
            print(f"    Kombi: n={combo_k['n']}  AvgR={combo_k['avg_r']:+.3f}  "
                  f"Delta={combo_delta:+.3f}R")
            if redundant:
                print(f"    ⚠️  {f2['filter']} ist redundant (Kombi ≈ F1 allein)")
            else:
                print(f"    ✅ Beide Filter sind nicht-redundant")
    else:
        print(f"  Kein Filter verbessert die Baseline signifikant.")
        print(f"  → Strategie läuft mit Basis-Parametern ohne Filter.")

    # Gate 2
    print(f"\n{'═'*82}")
    print(f"  GATE 2")
    g = len(keep_filters) >= 1
    print(f"  {'✅' if g else '🟡'} ≥ 1 Filter mit Delta ≥ +0.02R: "
          f"{'Ja' if g else 'Nein — weiter ohne Filter'}")
    print(f"  → {'GO: Phase 3 mit KEEP-Filtern' if g else 'GO: Phase 3 ohne Filter'}")
    print(f"{'═'*82}\n")


if __name__ == "__main__":
    main()
