#!/usr/bin/env python3
"""
Validate Backtest — vergleicht Backtest-Replay mit echten Live-Trades.

Läuft Backtest über die Live-Periode (2026-04-06..heute) und prüft ob die
simulierten Trades die echten rekonstruieren. Akzeptanz-Kriterien:
  - ≥ 80% der Live-Trades im Backtest wiedergefunden
  - |Avg-R-Differenz| ≤ 0.20R (kein systematischer Bias)

Gibt die 3-5 größten Abweichungen aus (zur Kalibrierung des Slippage-Modells).

Verwendung:
  python3 scripts/backtest/validate_backtest.py
  python3 scripts/backtest/validate_backtest.py --from 2026-04-06 --to 2026-04-20 --tolerance 300
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.backtest_engine import ORBBacktester

TRADES_FILE = os.path.join(PROJECT_DIR, "data", "trades.json")

MATCH_TOLERANCE_MIN = 10   # Live-Trade gilt als gefunden wenn Backtest-Trade ≤ N Minuten abweicht


def load_live_trades(from_date: str, to_date: str) -> list[dict]:
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    result = []
    for t in trades:
        if t.get("exit_pnl_r") is None:
            continue
        ts = t.get("timestamp", "")[:10]
        if from_date <= ts <= to_date:
            result.append(t)
    return result


def match_trades(live: list[dict], simulated: list[dict], tolerance_min: int) -> list[dict]:
    """
    Versucht jeden Live-Trade einem Simulations-Trade zuzuordnen.
    Match-Kriterium: gleicher Asset + gleiche Richtung + Timestamp ≤ tolerance_min auseinander.
    """
    matches = []
    used_sim = set()

    for lt in live:
        lt_ts = datetime.fromisoformat(lt["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
        lt_asset = lt.get("asset", "")
        lt_dir   = lt.get("direction", "")
        lt_r     = lt.get("exit_pnl_r")

        best_sim = None
        best_delta = float("inf")

        for i, st in enumerate(simulated):
            if i in used_sim:
                continue
            if st["asset"] != lt_asset or st["direction"] != lt_dir:
                continue
            st_ts = datetime.fromisoformat(st["timestamp"]).replace(tzinfo=None)
            delta_min = abs((lt_ts - st_ts).total_seconds()) / 60
            if delta_min <= tolerance_min and delta_min < best_delta:
                best_delta = delta_min
                best_sim = (i, st)

        if best_sim:
            idx, st = best_sim
            used_sim.add(idx)
            matches.append({
                "asset":       lt_asset,
                "direction":   lt_dir,
                "live_ts":     lt["timestamp"][:16],
                "sim_ts":      st["timestamp"][:16],
                "delta_min":   round(best_delta, 1),
                "live_r":      round(lt_r, 3),
                "sim_r":       round(st["exit_pnl_r"], 3) if st.get("exit_pnl_r") is not None else None,
                "r_diff":      round((st.get("exit_pnl_r") or 0) - lt_r, 3),
                "live_exit":   lt.get("exit_reason", "?"),
                "sim_exit":    st.get("exit_reason", "?"),
                "matched":     True,
            })
        else:
            matches.append({
                "asset":     lt_asset,
                "direction": lt_dir,
                "live_ts":   lt["timestamp"][:16],
                "live_r":    round(lt_r, 3) if lt_r is not None else None,
                "matched":   False,
            })

    return matches


def main():
    parser = argparse.ArgumentParser(description="APEX Backtest-Validator")
    parser.add_argument("--from", dest="start", default="2026-04-06",
                        help="Start der Live-Periode (default: 2026-04-06)")
    parser.add_argument("--to",   dest="end",   default="2026-04-20",
                        help="Ende der Live-Periode (default: 2026-04-20)")
    parser.add_argument("--tolerance", type=int, default=MATCH_TOLERANCE_MIN,
                        help=f"Max Zeitdifferenz in Minuten für Match (default: {MATCH_TOLERANCE_MIN})")
    args = parser.parse_args()

    print(f"🔍 Backtest-Validator: {args.start} → {args.end}")
    print()

    # Live-Trades laden
    live_trades = load_live_trades(args.start, args.end)
    if not live_trades:
        print("⚠️  Keine abgeschlossenen Live-Trades im Zeitraum gefunden.")
        sys.exit(1)
    print(f"   Live-Trades gefunden: {len(live_trades)}")

    # Backtest über gleichen Zeitraum
    print(f"   Starte Backtest-Replay...")
    backtester = ORBBacktester(verbose=False)
    result = backtester.run(args.start, args.end)
    sim_trades = result["trades"]
    print(f"   Backtest-Trades simuliert: {len(sim_trades)}")
    print()

    # Matching
    matches = match_trades(live_trades, sim_trades, args.tolerance)
    matched = [m for m in matches if m["matched"]]
    unmatched = [m for m in matches if not m["matched"]]

    match_rate = len(matched) / len(live_trades) if live_trades else 0

    # Statistik
    r_diffs = [m["r_diff"] for m in matched if m.get("r_diff") is not None]
    avg_diff = sum(r_diffs) / len(r_diffs) if r_diffs else 0
    live_avg = sum(t.get("exit_pnl_r", 0) for t in live_trades) / len(live_trades) if live_trades else 0
    sim_avg  = result["summary"].get("avg_r", 0)

    # Report
    print("=" * 60)
    print("  VALIDIERUNGS-REPORT")
    print("=" * 60)
    print(f"  Live-Trades:      {len(live_trades)}")
    print(f"  Gematchte:        {len(matched)} ({match_rate*100:.1f}%)")
    print(f"  Ungematchte:      {len(unmatched)}")
    print()
    print(f"  Live Avg-R:       {live_avg:+.3f}R")
    print(f"  Backtest Avg-R:   {sim_avg:+.3f}R")
    print(f"  Avg R-Differenz:  {avg_diff:+.3f}R  (sim - live)")
    print()

    # Akzeptanz-Check
    ok_match = match_rate >= 0.80
    ok_diff  = abs(avg_diff) <= 0.20
    print(f"  Match-Rate ≥80%:  {'✅' if ok_match else '❌'} ({match_rate*100:.1f}%)")
    print(f"  |Avg-Diff| ≤0.2R: {'✅' if ok_diff  else '❌'} ({abs(avg_diff):.3f}R)")

    if ok_match and ok_diff:
        print("\n  ✅ Backtest-Pipeline VALIDIERT — Daten vertrauenswürdig.")
    else:
        print("\n  ⚠️  Validierung FEHLGESCHLAGEN — Slippage-Modell prüfen.")

    # Größte Abweichungen
    if matched:
        worst = sorted(matched, key=lambda m: abs(m.get("r_diff", 0)), reverse=True)[:5]
        print("\n  Größte Abweichungen (sim - live):")
        print(f"  {'Datum':<17} {'Asset':<5} {'Dir':<6} {'LiveR':>7} {'SimR':>7} {'Diff':>7} {'LiveExit':<14} {'SimExit'}")
        for m in worst:
            print(f"  {m['live_ts']:<17} {m['asset']:<5} {m['direction']:<6} "
                  f"{m['live_r']:>+7.3f} {(m.get('sim_r') or 0):>+7.3f} {m['r_diff']:>+7.3f} "
                  f"{m['live_exit']:<14} {m['sim_exit']}")

    # Nicht-gematchte Live-Trades
    if unmatched:
        print(f"\n  Nicht-gematchte Live-Trades ({len(unmatched)}):")
        for m in unmatched:
            print(f"    {m['live_ts']} {m['asset']} {m['direction']} → {m.get('live_r', '?')}R")

    print("=" * 60)


if __name__ == "__main__":
    main()
