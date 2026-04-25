#!/usr/bin/env python3
"""
Phase 0.4 — Time-of-Day-Analyse für PDH/PDL-Trades.

Aggregiert Avg R pro Entry-Stunde (UTC, 0-23). Zusätzlich:
  - Wochentag-Breakdown (0=Mo..6=So)
  - Asset-Heatmap 24h × Asset
  - CI95 für Avg R pro Stunde (n ≥ 20 für Aussagekraft)

Liest trade_mae_mfe.jsonl (aus Phase 0.2).

Verwendung:
  python3 scripts/backtest/time_of_day_analysis.py
  python3 scripts/backtest/time_of_day_analysis.py --exit-mode fixed_tp --tp-r 0.5
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.regime_breakdown import resolve_r, load_trades


def ci95(values: list[float]) -> tuple[float, float, float]:
    """Returns (mean, lower, upper) 95% CI via t-approximation (z=1.96)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, mean, mean
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    sd  = math.sqrt(var)
    se  = sd / math.sqrt(n)
    return mean, mean - 1.96 * se, mean + 1.96 * se


def dow_name(dow: int) -> str:
    return ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][dow]


def print_hour_table(rs_by_hour: dict):
    print(f"\n  === Avg R pro UTC-Stunde ===")
    print(f"  {'UTC':>4} {'n':>5} {'WR':>6} {'AvgR':>9} {'CI95_low':>9} {'CI95_hi':>9}  Signal")
    print(f"  {'-'*4} {'-'*5} {'-'*6} {'-'*9} {'-'*9} {'-'*9}  {'-'*25}")
    for hr in range(24):
        rs = rs_by_hour.get(hr, [])
        if not rs:
            print(f"  {hr:>4} {'—':>5}")
            continue
        mean, lo, hi = ci95(rs)
        wr = sum(1 for r in rs if r > 0) / len(rs)
        bar_len = int(abs(mean) * 100)
        bar = ("█" if mean > 0 else "▒") * min(bar_len, 30)
        sign = "✅" if (lo > 0 and len(rs) >= 20) else \
               ("❌" if (hi < 0 and len(rs) >= 20) else "  ")
        print(f"  {hr:>4} {len(rs):>5} {wr*100:>5.1f}% {mean:>+8.3f}R "
              f"{lo:>+8.3f}R {hi:>+8.3f}R  {sign} {bar}")


def print_dow_table(rs_by_dow: dict):
    print(f"\n  === Avg R pro Wochentag (UTC) ===")
    print(f"  {'Tag':<4} {'n':>5} {'WR':>6} {'AvgR':>9} {'TotalR':>10}")
    print(f"  {'-'*4} {'-'*5} {'-'*6} {'-'*9} {'-'*10}")
    for d in range(7):
        rs = rs_by_dow.get(d, [])
        if not rs:
            continue
        mean, _, _ = ci95(rs)
        wr = sum(1 for r in rs if r > 0) / len(rs)
        total = sum(rs)
        flag = "✅" if mean > 0.02 else ("⚠️" if mean > -0.02 else "❌")
        print(f"  {flag} {dow_name(d):<3} {len(rs):>5} {wr*100:>5.1f}% "
              f"{mean:>+8.3f}R {total:>+9.2f}R")


def print_asset_hour_heatmap(annotated: list[dict]):
    """
    ASCII-Heatmap 24h × Asset. Jede Zelle zeigt Avg R (verkürzt).
    Farbkodierung mit Symbol: + / 0 / - / █
    """
    assets = sorted({t["asset"] for t in annotated})
    print(f"\n  === Asset × Stunde Heatmap (Avg R, n≥5 dargestellt) ===")
    header = "  " + "UTC:" + "".join(f"{h:>3}" for h in range(24))
    print(header)
    for asset in assets:
        cells = []
        for hr in range(24):
            rs = [t["r"] for t in annotated if t["asset"] == asset and t["entry_hour"] == hr]
            if len(rs) < 5:
                cells.append(" . ")
                continue
            avg = sum(rs) / len(rs)
            if avg >= 0.10:
                cells.append(" ▲ ")
            elif avg >= 0.02:
                cells.append(" + ")
            elif avg > -0.02:
                cells.append(" 0 ")
            elif avg > -0.10:
                cells.append(" - ")
            else:
                cells.append(" ▼ ")
        print(f"  {asset:<5}   {''.join(cells)}")
    print(f"\n  Legende: ▲ ≥+0.10R  + ≥+0.02R  0 ±0.02R  - ≥-0.10R  ▼ <-0.10R  . <5 Trades")


def main():
    parser = argparse.ArgumentParser(description="Time-of-Day-Analyse für PDH/PDL")
    parser.add_argument("--exit-mode", default="baseline_2r",
                        choices=["baseline_2r", "fixed_tp", "mfe_peak"])
    parser.add_argument("--tp-r", type=float, default=0.5)
    args = parser.parse_args()

    print("⏰ Time-of-Day-Analyse")
    print(f"   Exit-Modus: {args.exit_mode}" +
          (f" (TP={args.tp_r}R)" if args.exit_mode == "fixed_tp" else ""))

    trades = load_trades()
    print(f"   Trades: {len(trades)}")

    rs_by_hour = {}
    rs_by_dow  = {}
    annotated = []
    for t in trades:
        r = resolve_r(t, args.exit_mode, args.tp_r)
        hr = t["entry_hour"]
        rs_by_hour.setdefault(hr, []).append(r)
        # Wochentag ableiten aus day
        dt = datetime.strptime(t["day"], "%Y-%m-%d")
        dow = dt.weekday()
        rs_by_dow.setdefault(dow, []).append(r)
        t2 = dict(t); t2["r"] = r
        annotated.append(t2)

    print_hour_table(rs_by_hour)
    print_dow_table(rs_by_dow)
    print_asset_hour_heatmap(annotated)

    # Highlight: signifikant negative Stunden
    print(f"\n  === Signifikant negative Stunden (CI95-Oberkante < 0, n≥20) ===")
    bad_hours = []
    for hr, rs in rs_by_hour.items():
        if len(rs) < 20:
            continue
        mean, lo, hi = ci95(rs)
        if hi < 0:
            bad_hours.append((hr, mean, lo, hi, len(rs)))
    if not bad_hours:
        print("  (keine Stunde mit CI95 komplett unter 0)")
    else:
        for hr, mean, lo, hi, n in sorted(bad_hours, key=lambda x: x[1]):
            print(f"  UTC {hr:>2}h:  AvgR={mean:+.3f}R  CI95=[{lo:+.3f}, {hi:+.3f}]  n={n}")

    print(f"\n  === Signifikant positive Stunden (CI95-Unterkante > 0, n≥20) ===")
    good_hours = []
    for hr, rs in rs_by_hour.items():
        if len(rs) < 20:
            continue
        mean, lo, hi = ci95(rs)
        if lo > 0:
            good_hours.append((hr, mean, lo, hi, len(rs)))
    if not good_hours:
        print("  (keine Stunde mit CI95 komplett über 0)")
    else:
        for hr, mean, lo, hi, n in sorted(good_hours, key=lambda x: -x[1]):
            print(f"  UTC {hr:>2}h:  AvgR={mean:+.3f}R  CI95=[{lo:+.3f}, {hi:+.3f}]  n={n}")


if __name__ == "__main__":
    main()
