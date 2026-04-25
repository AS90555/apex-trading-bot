#!/usr/bin/env python3
"""
Session-Experiment — testet 3 alternative Session-Definitionen für Krypto-ORB.

Test A: Tokyo as-is (Baseline bestätigen, 12-Monats-Periode)
Test B: Asia Box → London Open (Box 00:00–07:00 UTC, Entry 08:00–10:00 Berlin)
Test C: UTC Midnight ORB (Box erste 30min nach 00:00 UTC, Entry danach)

Verwendung:
  python3 scripts/backtest/session_experiment.py
  python3 scripts/backtest/session_experiment.py --from 2025-10-20 --to 2026-04-20
"""
import argparse
import os
import sys

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

import scripts.backtest.backtest_engine as engine
from scripts.backtest.backtest_engine import ORBBacktester


# ─── Session-Definitionen (alle Zeiten in Berlin-Zeit) ───────────────────────
# box_h/box_m = Zeitpunkt der Box-Schließung (letzte 15m-Candle davor)
# scan_start/scan_end = Stundenfenster für Breakout-Scan

SESSION_CONFIGS = {
    "A_tokyo_baseline": {
        "tokyo": {"box_h": 2,  "box_m": 15, "scan_start": 2,  "scan_end": 4},
    },
    "B_asia_box_london_open": {
        # Box schließt 07:15 Berlin (= 06:15 UTC Winter / 05:15 UTC Sommer)
        # d.h. Box = gesamte Asia-Nacht-Konsolidierung
        # Entry: London Open 08:00–10:00 Berlin
        "asia_london": {"box_h": 7, "box_m": 15, "scan_start": 8, "scan_end": 10},
    },
    "C_utc_midnight_orb": {
        # Box schließt 01:15 Berlin (= 00:15 UTC Winter) — erste 15m nach UTC-Midnight
        # Entry: 01:15–04:00 Berlin (=~00:15–03:00 UTC)
        "midnight_orb": {"box_h": 1, "box_m": 15, "scan_start": 1, "scan_end": 4},
    },
}


def run_test(name: str, sessions: dict, start: str, end: str, verbose: bool) -> dict:
    """Führt einen Backtest mit überschriebenen Sessions durch."""
    # Session-Dict in der Engine überschreiben
    original = engine.SESSIONS.copy()
    engine.SESSIONS = sessions

    backtester = ORBBacktester(verbose=verbose)
    result = backtester.run(start, end)

    engine.SESSIONS = original  # restore
    return result


def print_result(name: str, result: dict):
    s = result["summary"]
    print(f"\n{'─'*55}")
    print(f"  Test: {name}")
    print(f"{'─'*55}")
    if s["n_trades"] == 0:
        print("  ⚠️  Keine Trades simuliert")
        return
    print(f"  Trades:        {s['n_trades']}")
    print(f"  Win-Rate:      {s['win_rate']*100:.1f}%")
    print(f"  Avg R:         {s['avg_r']:+.3f}R")
    print(f"  Total R:       {s['total_r']:+.2f}R")
    print(f"  Profit Factor: {s['profit_factor']:.2f}")
    print(f"  Max Drawdown:  {s['max_drawdown_r']:.2f}R")

    # Session-Breakdown aus Trades
    session_stats = {}
    for t in result["trades"]:
        sess = t.get("session", "?")
        r    = t.get("exit_pnl_r", 0) or 0
        if sess not in session_stats:
            session_stats[sess] = {"n": 0, "total_r": 0.0, "wins": 0}
        session_stats[sess]["n"] += 1
        session_stats[sess]["total_r"] += r
        if r > 0:
            session_stats[sess]["wins"] += 1

    if session_stats:
        print(f"\n  Session-Breakdown:")
        for sess, st in session_stats.items():
            avg = st["total_r"] / st["n"]
            wr  = st["wins"] / st["n"] * 100
            print(f"    {sess:<20} n={st['n']:>3}  avg={avg:+.3f}R  WR={wr:.0f}%")


def main():
    parser = argparse.ArgumentParser(description="APEX Session-Experiment")
    parser.add_argument("--from", dest="start", default="2025-10-20")
    parser.add_argument("--to",   dest="end",   default="2026-04-20")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    verbose = not args.quiet

    print(f"🧪 Session-Experiment: {args.start} → {args.end}")
    print(f"   3 Tests: A (Tokyo Baseline) | B (Asia Box→London) | C (UTC Midnight)")
    print()

    results = {}
    for name, sessions in SESSION_CONFIGS.items():
        if verbose:
            print(f"\n▶ Starte {name}...")
        results[name] = run_test(name, sessions, args.start, args.end, verbose=False)

    # Vergleichs-Report
    print(f"\n{'═'*55}")
    print(f"  SESSION-EXPERIMENT VERGLEICH ({args.start} → {args.end})")
    print(f"{'═'*55}")
    print(f"  {'Test':<30} {'n':>4} {'AvgR':>7} {'WR':>6} {'TotalR':>8} {'PF':>5}")
    print(f"  {'─'*30} {'─'*4} {'─'*7} {'─'*6} {'─'*8} {'─'*5}")

    for name, result in results.items():
        s = result["summary"]
        if s["n_trades"] == 0:
            print(f"  {name:<30} {'—':>4}")
            continue
        print(f"  {name:<30} {s['n_trades']:>4} {s['avg_r']:>+7.3f} "
              f"{s['win_rate']*100:>5.1f}% {s['total_r']:>+8.2f} {s['profit_factor']:>5.2f}")

    # Detaillierte Einzel-Reports
    for name, result in results.items():
        print_result(name, result)

    print(f"\n{'═'*55}")
    print("  Interpretation:")
    for name, result in results.items():
        s = result["summary"]
        if s["n_trades"] == 0:
            continue
        if s["avg_r"] > 0:
            print(f"  ✅ {name}: positiver EV (+{s['avg_r']:.3f}R avg)")
        elif s["avg_r"] > -0.1:
            print(f"  ⚠️  {name}: nahezu Break-Even ({s['avg_r']:+.3f}R avg)")
        else:
            print(f"  ❌ {name}: negativer EV ({s['avg_r']:+.3f}R avg)")
    print(f"{'═'*55}")


if __name__ == "__main__":
    main()
