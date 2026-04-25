#!/usr/bin/env python3
"""
Phase 1.2 — Walk-Forward-Runner.

Rolling-Window-WFA:
  - IS-Fenster: 6 Monate (konfigurierbar)
  - OOS-Fenster: 1 Monat
  - Schritt: 1 Monat → 6-7 Folds über 12 Monate

Pro Fold: optimiere Parameter auf IS-Daten, teste auf OOS-Daten.
Metriken: WFE (Walk-Forward Efficiency), Anzahl OOS-profitabler Folds.

Verwendung:
  python3 scripts/backtest/walk_forward.py
  python3 scripts/backtest/walk_forward.py --exit-mode fixed_tp --grid 0.3,0.5,0.75,1.0,1.5
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from config import backtest_config as cfg
from scripts.backtest.regime_breakdown import load_trades, resolve_r


def parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def add_months(dt: datetime, months: int) -> datetime:
    year  = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    try:
        return dt.replace(year=year, month=month)
    except ValueError:
        return dt.replace(year=year, month=month, day=28)


def generate_folds(start: str, end: str,
                   is_months: int, oos_months: int, step_months: int) -> list[dict]:
    """Rolling IS/OOS-Folds über den Datenbereich."""
    folds = []
    cursor = parse_day(start)
    end_dt = parse_day(end)
    while True:
        is_start  = cursor
        is_end    = add_months(is_start, is_months) - timedelta(days=1)
        oos_start = add_months(is_start, is_months)
        oos_end   = add_months(oos_start, oos_months) - timedelta(days=1)
        if oos_end > end_dt:
            break
        folds.append({
            "is_start":  is_start.strftime("%Y-%m-%d"),
            "is_end":    is_end.strftime("%Y-%m-%d"),
            "oos_start": oos_start.strftime("%Y-%m-%d"),
            "oos_end":   oos_end.strftime("%Y-%m-%d"),
        })
        cursor = add_months(cursor, step_months)
    return folds


def filter_trades_by_date(trades: list[dict], start: str, end: str) -> list[dict]:
    return [t for t in trades if start <= t["day"] <= end]


def avg_r(trades: list[dict], exit_mode: str, tp_r: float) -> float:
    if not trades:
        return 0.0
    rs = [resolve_r(t, exit_mode, tp_r) for t in trades]
    return sum(rs) / len(rs)


def optimize_on_is(is_trades: list[dict], exit_mode: str,
                   grid: list[float]) -> dict:
    """
    Grid-Search auf IS-Daten. Für fixed_tp: welches TP-Level maximiert Avg R?
    Für baseline_2r / mfe_peak: kein Grid, nur Evaluation.
    """
    if exit_mode != "fixed_tp" or not grid:
        r = avg_r(is_trades, exit_mode, 0.5)
        return {"best_param": None, "best_is_r": r}

    best = None
    for tp in grid:
        r = avg_r(is_trades, "fixed_tp", tp)
        if best is None or r > best["best_is_r"]:
            best = {"best_param": tp, "best_is_r": r}
    return best


def run_wfa(trades: list[dict], exit_mode: str, grid: list[float] = None,
            is_months: int = None, oos_months: int = None,
            step_months: int = None,
            overall_start: str = None, overall_end: str = None) -> dict:
    is_months   = is_months   or cfg.WFA_IS_MONTHS
    oos_months  = oos_months  or cfg.WFA_OOS_MONTHS
    step_months = step_months or cfg.WFA_STEP_MONTHS
    overall_start = overall_start or cfg.DATA_START
    overall_end   = overall_end   or cfg.DATA_END

    folds = generate_folds(overall_start, overall_end,
                           is_months, oos_months, step_months)

    fold_results = []
    for i, fold in enumerate(folds, 1):
        is_trades  = filter_trades_by_date(trades, fold["is_start"],  fold["is_end"])
        oos_trades = filter_trades_by_date(trades, fold["oos_start"], fold["oos_end"])

        opt = optimize_on_is(is_trades, exit_mode, grid or [])
        is_r  = opt["best_is_r"]
        param = opt["best_param"]

        oos_r = avg_r(oos_trades, exit_mode,
                      param if param is not None else 0.5)

        fold_results.append({
            "fold":       i,
            "is_range":   f"{fold['is_start']} → {fold['is_end']}",
            "oos_range":  f"{fold['oos_start']} → {fold['oos_end']}",
            "n_is":       len(is_trades),
            "n_oos":      len(oos_trades),
            "best_param": param,
            "is_avg_r":   is_r,
            "oos_avg_r":  oos_r,
        })

    # WFE = mean(OOS) / mean(IS) — nur wenn IS positiv
    total_is  = sum(f["is_avg_r"]  for f in fold_results) / len(fold_results) if fold_results else 0
    total_oos = sum(f["oos_avg_r"] for f in fold_results) / len(fold_results) if fold_results else 0
    wfe = (total_oos / total_is) if total_is > 0 else None

    positive_oos = sum(1 for f in fold_results if f["oos_avg_r"] > 0)

    return {
        "folds":          fold_results,
        "mean_is_r":      total_is,
        "mean_oos_r":     total_oos,
        "wfe":            wfe,
        "positive_folds": positive_oos,
        "total_folds":    len(fold_results),
    }


def print_wfa_report(result: dict, exit_mode: str):
    print(f"\n  === Walk-Forward-Analysis (Exit: {exit_mode}) ===")
    print(f"  {'Fold':<4} {'IS-Range':<25} {'OOS-Range':<25} {'n_IS':>5} {'n_OOS':>5} "
          f"{'Param':>7} {'IS R':>9} {'OOS R':>9}")
    print(f"  {'-'*4} {'-'*25} {'-'*25} {'-'*5} {'-'*5} {'-'*7} {'-'*9} {'-'*9}")
    for f in result["folds"]:
        p = "—" if f["best_param"] is None else f"{f['best_param']:.2f}"
        icon = "✅" if f["oos_avg_r"] > 0 else "❌"
        print(f"  {f['fold']:<4} {f['is_range']:<25} {f['oos_range']:<25} "
              f"{f['n_is']:>5} {f['n_oos']:>5} {p:>7} "
              f"{f['is_avg_r']:>+8.4f}R {icon}{f['oos_avg_r']:>+7.4f}R")

    print(f"\n  Mean IS Avg R:     {result['mean_is_r']:>+8.4f}R")
    print(f"  Mean OOS Avg R:    {result['mean_oos_r']:>+8.4f}R")
    if result["wfe"] is not None:
        wfe_icon = "✅" if result["wfe"] >= cfg.WFA_ACCEPT_WFE else "❌"
        print(f"  {wfe_icon} WFE (OOS/IS):     {result['wfe']:.3f}  (Akzeptanz ≥ {cfg.WFA_ACCEPT_WFE})")
    else:
        print(f"  ⚠️  WFE: nicht berechenbar (IS ≤ 0)")

    pos_icon = "✅" if result["positive_folds"] >= cfg.WFA_MIN_FOLDS_POSITIVE else "❌"
    print(f"  {pos_icon} Positive OOS-Folds: {result['positive_folds']}/{result['total_folds']} "
          f"(Akzeptanz ≥ {cfg.WFA_MIN_FOLDS_POSITIVE})")


def main():
    parser = argparse.ArgumentParser(description="Walk-Forward-Analysis")
    parser.add_argument("--exit-mode", default="baseline_2r",
                        choices=["baseline_2r", "fixed_tp", "mfe_peak"])
    parser.add_argument("--grid", default="",
                        help="Komma-Liste von TP-Levels für fixed_tp (z.B. 0.3,0.5,0.75,1.0,1.5)")
    parser.add_argument("--is-months",   type=int, default=cfg.WFA_IS_MONTHS)
    parser.add_argument("--oos-months",  type=int, default=cfg.WFA_OOS_MONTHS)
    parser.add_argument("--step-months", type=int, default=cfg.WFA_STEP_MONTHS)
    args = parser.parse_args()

    grid = [float(x) for x in args.grid.split(",")] if args.grid else []

    print(f"🚶 Walk-Forward-Analysis")
    print(f"   Exit-Modus: {args.exit_mode}")
    if args.exit_mode == "fixed_tp" and grid:
        print(f"   TP-Grid:    {grid}")
    print(f"   IS: {args.is_months}m, OOS: {args.oos_months}m, Step: {args.step_months}m")

    trades = load_trades()
    print(f"   Trades: {len(trades)}")

    result = run_wfa(trades, args.exit_mode, grid,
                     args.is_months, args.oos_months, args.step_months)
    print_wfa_report(result, args.exit_mode)


if __name__ == "__main__":
    main()
