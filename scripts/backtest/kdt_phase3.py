#!/usr/bin/env python3
"""
KDT Phase 3 — Asset-Selektion.

Basis: EMA=50, Entry-Window=2, TP=3R, SHORT-only, F-04 Tight-SL (k=1.0).
Jedes Asset einzeln: IS + Quartals-Breakdown + OOS-Vorschau (nur für Entscheidung).

Klassifikation:
  KEEP   : n≥15, IS AvgR>0, PF>1.3, OOS AvgR>0, ≥2/4 Quartale positiv
  TOXIC  : IS AvgR<-0.1 ODER OOS AvgR<-0.2 ODER alle Quartale negativ
  NEUTRAL: alles andere

Verwendung:
  python3 scripts/backtest/kdt_phase3.py
"""
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
from scripts.backtest.kdt_phase2    import build_full_indicators, run_kdt_filtered

IS_START  = "2025-04-21"
IS_END    = "2026-02-10"
OOS_START = "2026-02-11"
OOS_END   = "2026-04-19"

QUARTERS = [
    ("2025-04-21", "2025-07-20"),
    ("2025-07-21", "2025-10-20"),
    ("2025-10-21", "2026-01-20"),
    ("2026-01-21", "2026-02-10"),
]

ALL_ASSETS = [
    "NEAR", "ETH", "DOGE", "AAVE", "TIA", "INJ", "APT",  # Kandidaten aus Phase 0
    "SOL", "AVAX", "BTC", "XRP", "LINK", "BNB", "ADA",   # Weitere zum Vergleich
    "SUI", "PEPE", "ARB", "OP", "LDO", "WIF", "SEI", "JUP",
]

EMA_PERIOD   = 50
ENTRY_WINDOW = 2
TP_R         = 3.0


def tight_sl_filter(c0, c1, c2, ind, i):
    """F-04: SL-Distanz < 1.0 × ATR(14)"""
    atr = ind[i]["atr14"]
    if atr <= 0:
        return True  # Fail-safe: kein Block bei fehlendem ATR
    sl_dist = c0["high"] - c0["low"]
    return sl_dist < 1.0 * atr


def kpis(r_list):
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "pf": 0.0, "total_r": 0.0, "p": 1.0}
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
            "pf": round(gw/gl, 2) if gl > 0 else float("inf"),
            "total_r": round(total, 2), "p": round(p, 4)}


def classify(is_k, oos_k, q_pos):
    if is_k["avg_r"] < -0.1 or oos_k["avg_r"] < -0.2 or q_pos == 0:
        return "TOXIC"
    if (is_k["n"] >= 15 and is_k["avg_r"] > 0 and is_k["pf"] > 1.3
            and oos_k["avg_r"] > 0 and q_pos >= 2):
        return "KEEP"
    return "NEUTRAL"


def main():
    print(f"\n{'═'*90}")
    print(f"  KDT Phase 3 — Asset-Selektion")
    print(f"  Basis: EMA={EMA_PERIOD} Win={ENTRY_WINDOW} TP={TP_R}R SHORT + F-04 Tight-SL")
    print(f"  IS: {IS_START}→{IS_END}  |  OOS: {OOS_START}→{OOS_END}")
    print(f"{'═'*90}\n")

    keep    = []
    toxic   = []
    neutral = []

    for asset in ALL_ASSETS:
        raw = load_csv(asset, "15m")
        if not raw:
            print(f"  {asset:<6}: keine Daten")
            continue
        candles = aggregate_1h(raw)
        inds    = build_full_indicators(candles)

        # IS
        is_r  = run_kdt_filtered(candles, inds, IS_START, IS_END, tight_sl_filter)
        is_k  = kpis(is_r)

        # OOS (nur zur Klassifikation, nicht zur Optimierung)
        oos_r = run_kdt_filtered(candles, inds, OOS_START, OOS_END, tight_sl_filter)
        oos_k = kpis(oos_r)

        # Quartale
        q_results = []
        for qs, qe in QUARTERS:
            qr = run_kdt_filtered(candles, inds, qs, qe, tight_sl_filter)
            q_results.append(sum(qr) if qr else 0.0)
        q_pos = sum(1 for q in q_results if q > 0)

        verdict = classify(is_k, oos_k, q_pos)
        icons   = {"KEEP": "✅", "TOXIC": "❌", "NEUTRAL": "🟡"}
        q_str   = " ".join(f"{q:+.1f}" for q in q_results)

        print(f"  {icons[verdict]} {asset:<6}  "
              f"IS: n={is_k['n']:>3}  AvgR={is_k['avg_r']:>+.3f}  "
              f"WR={is_k['wr']*100:>4.1f}%  PF={is_k['pf']:>4.1f}  "
              f"| OOS: n={oos_k['n']:>2}  AvgR={oos_k['avg_r']:>+.3f}  "
              f"| Q:[{q_str}] {q_pos}/4")

        entry = {"asset": asset, "is_k": is_k, "oos_k": oos_k,
                 "q_pos": q_pos, "quarterly": q_results}
        if verdict == "KEEP":
            keep.append(entry)
        elif verdict == "TOXIC":
            toxic.append(entry)
        else:
            neutral.append(entry)

    # Ergebnis
    print(f"\n{'═'*90}")
    print(f"  ERGEBNIS\n")

    print(f"  ✅ KEEP ({len(keep)}):")
    for e in sorted(keep, key=lambda x: -x["is_k"]["avg_r"]):
        print(f"     {e['asset']:<6}  IS AvgR={e['is_k']['avg_r']:>+.3f}  "
              f"n={e['is_k']['n']}  OOS={e['oos_k']['avg_r']:>+.3f}  Q:{e['q_pos']}/4")

    print(f"\n  🟡 NEUTRAL ({len(neutral)}):")
    for e in sorted(neutral, key=lambda x: -x["is_k"]["avg_r"]):
        print(f"     {e['asset']:<6}  IS AvgR={e['is_k']['avg_r']:>+.3f}  "
              f"n={e['is_k']['n']}  OOS={e['oos_k']['avg_r']:>+.3f}  Q:{e['q_pos']}/4")

    print(f"\n  ❌ TOXIC ({len(toxic)}):")
    for e in sorted(toxic, key=lambda x: x["is_k"]["avg_r"]):
        print(f"     {e['asset']:<6}  IS AvgR={e['is_k']['avg_r']:>+.3f}  "
              f"n={e['is_k']['n']}  OOS={e['oos_k']['avg_r']:>+.3f}  Q:{e['q_pos']}/4")

    # Gate 3
    print(f"\n{'═'*90}")
    print(f"  GATE 3")
    g = len(keep) >= 3
    print(f"  {'✅' if g else '❌'} ≥ 3 KEEP-Assets: {len(keep)}")
    if keep:
        assets_str = [e["asset"] for e in keep]
        print(f"  → Finale Asset-Liste: {assets_str}")
        print(f"  → {'GO: Phase 4 Robustheit' if g else 'NO-GO: zu wenige Assets'}")
    print(f"{'═'*90}\n")


if __name__ == "__main__":
    main()
