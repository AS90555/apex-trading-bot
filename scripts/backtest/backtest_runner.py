#!/usr/bin/env python3
"""
Backtest-Runner — CLI für APEX ORB Backtest-Engine.

Verwendung:
  python3 scripts/backtest/backtest_runner.py
  python3 scripts/backtest/backtest_runner.py --from 2025-10-01 --to 2026-04-20
  python3 scripts/backtest/backtest_runner.py --filters-off ema200_misaligned,low_volume
  python3 scripts/backtest/backtest_runner.py --fees none --quiet
  python3 scripts/backtest/backtest_runner.py --assets ETH,SOL --from 2025-10-01 --output data/backtest_results/run.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.backtest_engine import ORBBacktester

RESULTS_DIR = os.path.join(PROJECT_DIR, "data", "backtest_results")


def print_summary(summary: dict, filters_off: set, from_date: str, to_date: str):
    print()
    print("=" * 60)
    print(f"  APEX Backtest — {from_date} → {to_date}")
    if filters_off:
        print(f"  Filter deaktiviert: {', '.join(sorted(filters_off))}")
    print("=" * 60)
    if summary["n_trades"] == 0:
        print("  Keine Trades simuliert.")
        return
    print(f"  Trades:        {summary['n_trades']}")
    print(f"  Skips:         {summary['n_skips']}")
    print(f"  Win-Rate:      {summary['win_rate']*100:.1f}%")
    print(f"  Avg R:         {summary['avg_r']:+.3f}R")
    print(f"  Total R:       {summary['total_r']:+.2f}R")
    print(f"  Profit Factor: {summary['profit_factor']:.2f}")
    print(f"  Max Drawdown:  {summary['max_drawdown_r']:.2f}R")
    print()
    print("  Skip-Gründe:")
    for reason, count in sorted(summary["skip_reasons"].items(), key=lambda x: -x[1]):
        print(f"    {reason:<28} {count:>5}")
    print("=" * 60)


def print_equity_curve(trades: list, width: int = 50):
    r_vals = [t["exit_pnl_r"] for t in trades if t.get("exit_pnl_r") is not None]
    if not r_vals:
        return
    equity = []
    cumsum = 0.0
    for r in r_vals:
        cumsum += r
        equity.append(cumsum)

    min_e = min(equity)
    max_e = max(equity)
    rng   = max_e - min_e if max_e != min_e else 1.0

    print("\n  Equity-Kurve (ASCII):")
    height = 8
    rows = []
    for row in range(height, 0, -1):
        threshold = min_e + rng * (row / height)
        line = ""
        # Sample auf width Punkte
        step = max(1, len(equity) // width)
        for i in range(0, len(equity), step):
            line += "█" if equity[i] >= threshold else " "
        rows.append(f"  {threshold:+6.1f}R │{line}")
    rows.append(f"  {'':7} └{'─'*width}")
    print("\n".join(rows))
    print(f"  Start: 0R  →  Ende: {equity[-1]:+.2f}R")


def main():
    parser = argparse.ArgumentParser(description="APEX ORB Backtest-Runner")
    parser.add_argument("--from",    dest="start",   default="2025-10-20",
                        help="Startdatum YYYY-MM-DD (default: 2025-10-20)")
    parser.add_argument("--to",      dest="end",     default="2026-04-20",
                        help="Enddatum YYYY-MM-DD (default: 2026-04-20)")
    parser.add_argument("--assets",  default=None,
                        help="Komma-getrennte Assets (default: alle aus bot_config)")
    parser.add_argument("--filters-off", dest="filters_off", default="",
                        help="Komma-getrennte Filter die deaktiviert werden (z.B. ema200_misaligned,low_volume)")
    parser.add_argument("--fees",    default="standard",
                        choices=["none", "standard", "pessimistic"],
                        help="Fee-Szenario (default: standard)")
    parser.add_argument("--output",  default=None,
                        help="Ausgabedatei JSON (optional, sonst nur Print)")
    parser.add_argument("--quiet",   action="store_true",
                        help="Keine Trade-by-Trade Ausgabe")
    parser.add_argument("--equity",  action="store_true",
                        help="Equity-Kurve anzeigen")
    args = parser.parse_args()

    filters_off = {f.strip() for f in args.filters_off.split(",") if f.strip()}
    assets = [a.strip().upper() for a in args.assets.split(",")] if args.assets else None
    fee_model = (args.fees != "none")

    verbose = not args.quiet
    print(f"🔄 APEX Backtest: {args.start} → {args.end}")
    if filters_off:
        print(f"   Filter deaktiviert: {', '.join(sorted(filters_off))}")
    print(f"   Fee-Modell: {args.fees}")
    print()

    backtester = ORBBacktester(assets=assets, filters_off=filters_off,
                                fee_model=fee_model, verbose=verbose)

    result = backtester.run(args.start, args.end)

    print_summary(result["summary"], filters_off, args.start, args.end)

    if args.equity:
        print_equity_curve(result["trades"])

    # Ausgabe in JSON
    if args.output:
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        out_path = args.output
    else:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        filters_tag = "_no_" + "_".join(sorted(filters_off)) if filters_off else ""
        out_path = os.path.join(RESULTS_DIR, f"run_{ts}{filters_tag}.json")

    with open(out_path, "w") as f:
        json.dump({
            "meta": {
                "from": args.start,
                "to":   args.end,
                "filters_off": sorted(filters_off),
                "fee_model":   args.fees,
                "run_at":      datetime.now().isoformat(),
            },
            "summary": result["summary"],
            "trades":  result["trades"],
        }, f, indent=2, default=str)

    print(f"\n💾 Ergebnis gespeichert: {out_path}")


if __name__ == "__main__":
    main()
