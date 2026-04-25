#!/usr/bin/env python3
"""
Phase 1.4 — Monte Carlo Trade-Shuffle.

Input:  Liste von R-Werten pro Trade (in Reihenfolge)
Output: Verteilung über 10k Iterationen:
          - Final-Equity (Sum of R)
          - Max-Drawdown (als R)
          - Sharpe Ratio (trade-level)
          - 5/50/95-Perzentile

Verwendung (als Modul):
  from scripts.backtest.monte_carlo import run_monte_carlo
  result = run_monte_carlo(r_list, iterations=10000)

Als CLI gegen trade_mae_mfe.jsonl:
  python3 scripts/backtest/monte_carlo.py
  python3 scripts/backtest/monte_carlo.py --exit-mode fixed_tp --tp-r 0.5
"""
import argparse
import json
import math
import os
import random
import sys

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from config import backtest_config as cfg


def equity_curve(r_list: list[float]) -> list[float]:
    """Kumulative R-Summe (Startpunkt 0)."""
    curve = [0.0]
    acc = 0.0
    for r in r_list:
        acc += r
        curve.append(acc)
    return curve


def max_drawdown_r(r_list: list[float]) -> float:
    """Maximum Drawdown in R (als positiver Wert)."""
    curve = equity_curve(r_list)
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def sharpe(r_list: list[float]) -> float:
    """Trade-Level-Sharpe = mean(R) / stddev(R). Annualisierung separat."""
    n = len(r_list)
    if n < 2:
        return 0.0
    mean = sum(r_list) / n
    var  = sum((r - mean) ** 2 for r in r_list) / (n - 1)
    sd   = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd


def percentile(values: list[float], p: float) -> float:
    """p ∈ [0,100]. Lineare Interpolation."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run_monte_carlo(r_list: list[float], iterations: int = None,
                    seed: int = None, mode: str = "bootstrap") -> dict:
    """
    mode = "shuffle":   Permutation — Final-R & Sharpe invariant (nur Max-DD variiert)
    mode = "bootstrap": Resampling mit Replacement — alle 3 Metriken variieren
    """
    if iterations is None:
        iterations = cfg.MC_ITERATIONS
    if seed is None:
        seed = cfg.MC_SEED
    rng = random.Random(seed)
    n = len(r_list)

    finals   = []
    max_dds  = []
    sharpes  = []

    for _ in range(iterations):
        if mode == "shuffle":
            sample = r_list[:]
            rng.shuffle(sample)
        else:  # bootstrap
            sample = [r_list[rng.randrange(n)] for _ in range(n)]
        finals.append(sum(sample))
        max_dds.append(max_drawdown_r(sample))
        sharpes.append(sharpe(sample))

    # Realisierte (unshuffled) Werte
    realized = {
        "final_r":  sum(r_list),
        "max_dd_r": max_drawdown_r(r_list),
        "sharpe":   sharpe(r_list),
    }

    return {
        "iterations": iterations,
        "mode":       mode,
        "realized":   realized,
        "final_r": {
            "p5":  percentile(finals, 5),
            "p50": percentile(finals, 50),
            "p95": percentile(finals, 95),
            "mean": sum(finals) / iterations,
        },
        "max_dd_r": {
            "p5":  percentile(max_dds, 5),
            "p50": percentile(max_dds, 50),
            "p95": percentile(max_dds, 95),
            "mean": sum(max_dds) / iterations,
        },
        "sharpe": {
            "p5":  percentile(sharpes, 5),
            "p50": percentile(sharpes, 50),
            "p95": percentile(sharpes, 95),
            "mean": sum(sharpes) / iterations,
        },
    }


def check_acceptance(result: dict) -> dict:
    """
    Phase-5-Akzeptanzkriterien:
      - 5-Perzentil Final-Equity > 0
      - Realisierter Max-DD innerhalb 95-Perzentil der MC-Verteilung
      - 95-Perzentil Sharpe < 2× realisierter Sharpe (nicht zu "glatt")
    """
    rz = result["realized"]
    f5 = result["final_r"]["p5"]
    dd95 = result["max_dd_r"]["p95"]
    s95 = result["sharpe"]["p95"]

    checks = {
        "p5_final_positive":     f5 > 0,
        "realized_dd_in_bounds": rz["max_dd_r"] <= dd95 * 1.05,  # 5%-Tolerance
        "sharpe_not_too_smooth": s95 < 2 * abs(rz["sharpe"]) if rz["sharpe"] != 0 else True,
    }
    checks["all_passed"] = all(checks.values())
    return checks


def print_summary(result: dict):
    rz = result["realized"]
    print(f"\n  === Monte Carlo ({result['iterations']:,} Iterations, mode={result.get('mode', 'bootstrap')}) ===")
    print(f"  {'Kennzahl':<15} {'Realized':>10} {'P5':>10} {'P50':>10} {'P95':>10}")
    print(f"  {'-'*15} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for key, label, rz_key in [("final_r", "Final R", "final_r"),
                               ("max_dd_r", "Max DD R", "max_dd_r"),
                               ("sharpe", "Sharpe", "sharpe")]:
        r_val = rz[rz_key]
        p = result[key]
        print(f"  {label:<15} {r_val:>+10.3f} {p['p5']:>+10.3f} {p['p50']:>+10.3f} {p['p95']:>+10.3f}")

    checks = check_acceptance(result)
    print(f"\n  === Acceptance-Gates ===")
    for k, v in checks.items():
        if k == "all_passed":
            continue
        icon = "✅" if v else "❌"
        print(f"  {icon} {k}")
    final_icon = "✅" if checks["all_passed"] else "❌"
    print(f"  {final_icon} Gesamt: {'BESTANDEN' if checks['all_passed'] else 'VERFEHLT'}")


def main():
    parser = argparse.ArgumentParser(description="Monte Carlo Trade-Shuffle")
    parser.add_argument("--exit-mode", default="baseline_2r",
                        choices=["baseline_2r", "fixed_tp", "mfe_peak"])
    parser.add_argument("--tp-r", type=float, default=0.5)
    parser.add_argument("--iterations", type=int, default=cfg.MC_ITERATIONS)
    parser.add_argument("--mode", default="bootstrap",
                        choices=["bootstrap", "shuffle"])
    args = parser.parse_args()

    from scripts.backtest.regime_breakdown import load_trades, resolve_r
    trades = load_trades()
    r_list = [resolve_r(t, args.exit_mode, args.tp_r) for t in trades]

    print(f"🎲 Monte Carlo")
    print(f"   Exit-Modus: {args.exit_mode}" +
          (f" (TP={args.tp_r}R)" if args.exit_mode == "fixed_tp" else ""))
    print(f"   Trades: {len(r_list)}")
    print(f"   Iterationen: {args.iterations:,}")

    result = run_monte_carlo(r_list, iterations=args.iterations, mode=args.mode)
    print_summary(result)


if __name__ == "__main__":
    main()
