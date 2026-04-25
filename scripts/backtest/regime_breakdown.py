#!/usr/bin/env python3
"""
Phase 0.3 — Regime-Breakdown für PDH/PDL-Trades.

Klassifiziert jeden Trade nach BTC-30d-Rendite am Entry-Tag:
  bull_strong  >  +15%
  bull_quiet   +0..+15%
  sideways     -5..+5%   (überlappt bull_quiet/bear_quiet für robuste Abgrenzung)
  bear_quiet   -15..-5%
  bear_strong  <  -15%

Liest trade_mae_mfe.jsonl (aus Phase 0.2), berechnet für jeden Trade das Regime
und aggregiert Avg R / WR / PF pro Bucket.

Verwendung:
  python3 scripts/backtest/regime_breakdown.py
  python3 scripts/backtest/regime_breakdown.py --exit-mode mfe_peak
  python3 scripts/backtest/regime_breakdown.py --exit-mode fixed_tp --tp-r 0.5
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, aggregate_daily

TRADE_EXPORT = os.path.join(PROJECT_DIR, "data", "analysis", "trade_mae_mfe.jsonl")
REGIME_EXPORT = os.path.join(PROJECT_DIR, "data", "analysis", "trade_regimes.jsonl")


def classify_regime(btc_30d_return_pct: float) -> str:
    r = btc_30d_return_pct
    if r > 15:   return "bull_strong"
    if r > 5:    return "bull_quiet"
    if r > -5:   return "sideways"
    if r > -15:  return "bear_quiet"
    return "bear_strong"


def build_btc_regime_map() -> dict:
    """Erstellt date_str → regime_str Mapping aus BTC-15m-Candles."""
    candles = load_csv("BTC", "15m")
    if not candles:
        print("   FEHLER: keine BTC-Daten gefunden")
        return {}
    daily = aggregate_daily(candles)
    sorted_days = sorted(daily.keys())

    regime_map = {}
    for i, day in enumerate(sorted_days):
        # 30-Tage-Return = close(day) / close(day-30) - 1
        if i < 30:
            continue
        ref_day = sorted_days[i - 30]
        close_now = daily[day]["close"]
        close_ref = daily[ref_day]["close"]
        if close_ref <= 0:
            continue
        ret_pct = (close_now / close_ref - 1) * 100
        regime_map[day] = {"regime": classify_regime(ret_pct),
                           "btc_30d_ret_pct": round(ret_pct, 2)}
    return regime_map


def load_trades() -> list[dict]:
    if not os.path.exists(TRADE_EXPORT):
        print(f"   FEHLER: {TRADE_EXPORT} fehlt — erst mae_mfe_analysis.py laufen lassen")
        sys.exit(1)
    trades = []
    with open(TRADE_EXPORT) as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def resolve_r(trade: dict, exit_mode: str, tp_r: float) -> float:
    if exit_mode == "baseline_2r":
        # Baseline: SL=-1R, TP=2R, sonst final
        if trade["sl_hit"] and trade["tp_2r_hit"]:
            # Welcher zuerst? Konservativ SL zuerst
            if trade["sl_time_min"] and trade["tp_2r_time_min"]:
                if trade["sl_time_min"] <= trade["tp_2r_time_min"]:
                    return -1.0
                return 2.0
            return -1.0
        if trade["sl_hit"]:
            return -1.0
        if trade["tp_2r_hit"]:
            return 2.0
        return trade["final_r"]
    elif exit_mode == "fixed_tp":
        # SL=-1R, TP=tp_r wenn MFE≥tp_r vor MAE≤-1R erreicht
        # Approximation: wenn MFE ≥ tp_r UND MAE > -1R → tp_r
        # wenn MAE ≤ -1R → -1R
        # sonst final
        if trade["mfe_r"] >= tp_r and trade["mae_r"] > -1.0:
            return tp_r
        if trade["mae_r"] <= -1.0:
            return -1.0
        return trade["final_r"]
    elif exit_mode == "mfe_peak":
        return trade["mfe_r"]
    else:
        raise ValueError(f"unknown exit_mode: {exit_mode}")


def summarize_group(trades: list[dict], resolve_fn) -> dict:
    if not trades:
        return {"n": 0}
    rs = [resolve_fn(t) for t in trades]
    wins  = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total = sum(rs)
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    return {
        "n":       len(rs),
        "wins":    len(wins),
        "wr":      len(wins) / len(rs),
        "avg_r":   total / len(rs),
        "total_r": total,
        "pf":      pf,
    }


def print_breakdown(title: str, groups: dict):
    print(f"\n  === {title} ===")
    print(f"  {'Bucket':<15} {'n':>5} {'WR':>6} {'AvgR':>9} {'TotalR':>10} {'PF':>7}")
    print(f"  {'-'*15} {'-'*5} {'-'*6} {'-'*9} {'-'*10} {'-'*7}")
    order = ["bull_strong", "bull_quiet", "sideways", "bear_quiet", "bear_strong"]
    for key in order:
        if key not in groups:
            continue
        s = groups[key]
        if s["n"] == 0:
            continue
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "∞"
        flag = "✅" if s['avg_r'] > 0.02 else ("⚠️" if s['avg_r'] > -0.02 else "❌")
        print(f"  {flag} {key:<13} {s['n']:>5} {s['wr']*100:>5.1f}% "
              f"{s['avg_r']:>+8.3f}R {s['total_r']:>+9.2f}R {pf_str:>7}")


def main():
    parser = argparse.ArgumentParser(description="Regime-Breakdown für PDH/PDL-Trades")
    parser.add_argument("--exit-mode", default="baseline_2r",
                        choices=["baseline_2r", "fixed_tp", "mfe_peak"],
                        help="Wie Exit simulieren (baseline_2r = aus trade_mae_mfe)")
    parser.add_argument("--tp-r", type=float, default=0.5,
                        help="TP-Level für fixed_tp Modus (default 0.5R)")
    args = parser.parse_args()

    print("🌀 Regime-Breakdown")
    print(f"   Exit-Modus: {args.exit_mode}" +
          (f" (TP={args.tp_r}R)" if args.exit_mode == "fixed_tp" else ""))

    regime_map = build_btc_regime_map()
    if not regime_map:
        sys.exit(1)

    trades = load_trades()
    print(f"   Trades: {len(trades)}, BTC-Tage mit Regime: {len(regime_map)}")

    resolve_fn = lambda t: resolve_r(t, args.exit_mode, args.tp_r)

    # Annotate trades mit regime
    annotated = []
    by_regime = {}
    by_direction_regime = {}
    missing = 0
    for t in trades:
        day = t["day"]
        if day not in regime_map:
            missing += 1
            continue
        reg = regime_map[day]["regime"]
        t2 = dict(t)
        t2["regime"] = reg
        t2["btc_30d_ret_pct"] = regime_map[day]["btc_30d_ret_pct"]
        annotated.append(t2)
        by_regime.setdefault(reg, []).append(t2)
        key = f"{t2['direction']}_{reg}"
        by_direction_regime.setdefault(key, []).append(t2)

    if missing:
        print(f"   (⚠️  {missing} Trades ohne BTC-Regime-Match übersprungen)")

    # Summary pro Regime
    groups = {reg: summarize_group(tr, resolve_fn) for reg, tr in by_regime.items()}
    print_breakdown(f"Regime-Breakdown (Exit: {args.exit_mode})", groups)

    # Direction × Regime
    print(f"\n  === Direction × Regime ===")
    print(f"  {'Bucket':<22} {'n':>5} {'WR':>6} {'AvgR':>9} {'TotalR':>10}")
    print(f"  {'-'*22} {'-'*5} {'-'*6} {'-'*9} {'-'*10}")
    for reg in ["bull_strong", "bull_quiet", "sideways", "bear_quiet", "bear_strong"]:
        for dir_ in ["long", "short"]:
            key = f"{dir_}_{reg}"
            if key not in by_direction_regime:
                continue
            s = summarize_group(by_direction_regime[key], resolve_fn)
            if s["n"] == 0:
                continue
            flag = "✅" if s['avg_r'] > 0.02 else ("⚠️" if s['avg_r'] > -0.02 else "❌")
            print(f"  {flag} {dir_:<5}  {reg:<14} {s['n']:>5} {s['wr']*100:>5.1f}% "
                  f"{s['avg_r']:>+8.3f}R {s['total_r']:>+9.2f}R")

    # Export
    os.makedirs(os.path.dirname(REGIME_EXPORT), exist_ok=True)
    with open(REGIME_EXPORT, "w") as f:
        for t in annotated:
            f.write(json.dumps(t) + "\n")
    print(f"\n  Export: {REGIME_EXPORT}")


if __name__ == "__main__":
    main()
