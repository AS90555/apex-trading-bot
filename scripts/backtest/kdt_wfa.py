#!/usr/bin/env python3
"""
KDT Walk-Forward Analysis — Phase 1.

Grid-Search auf IS-Daten, OOS NIE zur Optimierung genutzt.
Rolling-Window WFA: 6-Monats-IS, 1-Monats-OOS, 6 Folds.

Verwendung:
  python3 scripts/backtest/kdt_wfa.py
  python3 scripts/backtest/kdt_wfa.py --direction short
"""
import argparse
import itertools
import math
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE
from scripts.backtest.squeeze_scout  import aggregate_1h
from scripts.backtest.kdt_scout     import detect_signal, _ema_series

# ─── IS/OOS Split (HEILIG — NIE ÄNDERN) ──────────────────────────────────────
IS_START  = "2025-04-21"
IS_END    = "2026-02-10"
OOS_START = "2026-02-11"
OOS_END   = "2026-04-19"

# ─── Asset-Universum (Phase-0-Gewinner, SHORT-only Kandidaten) ────────────────
CANDIDATE_ASSETS = ["NEAR", "ETH", "DOGE", "AAVE", "TIA", "INJ", "APT"]

# ─── Parameter-Grid (3×3×3 = 27 Kombinationen) ───────────────────────────────
GRID = {
    "ema_period":   [20, 50, 100],
    "entry_window": [1, 2, 3],
    "tp_r":         [2.0, 3.0, 4.0],
}
N_COMBINATIONS   = 27
ALPHA_BONFERRONI = 0.05 / N_COMBINATIONS  # 0.00185

# ─── WFA-Schema ───────────────────────────────────────────────────────────────
IS_MONTHS  = 6   # IS-Fenster
OOS_MONTHS = 1   # OOS-Fenster
N_FOLDS    = 6


# ─── Kern-Backtest ────────────────────────────────────────────────────────────

def run_kdt_params(candles_all: list[dict], start: str, end: str,
                   ema_period: int, entry_window: int, tp_r: float,
                   direction: str = "short") -> list[float]:
    """Schlanker Backtest für WFA — gibt R-Liste zurück."""
    closes  = [c["close"] for c in candles_all]
    ema_arr = _ema_series(closes, ema_period)
    warmup  = ema_period + 5

    pending  = []
    in_trade = False
    trade    = {}
    results  = []

    for i, c in enumerate(candles_all):
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")

        # Offene Position managen
        if in_trade:
            ae   = trade["ae"]
            sl   = trade["sl"]
            tp   = trade["tp"]
            risk = trade["risk"]
            d    = trade["direction"]

            hit_sl = c["high"] >= sl if d == "short" else c["low"] <= sl
            hit_tp = c["low"]  <= tp if d == "short" else c["high"] >= tp

            if hit_sl and not hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                results.append(-1.0 - fee_r)
                in_trade = False
                continue
            if hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                results.append(tp_r - fee_r)
                in_trade = False
                continue
            continue

        # Pending prüfen
        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"]:
                    continue
                if day < start or day > end:
                    continue
                d = p["direction"]
                fired = (d == "short" and c["low"]  <= p["stop"]) or \
                        (d == "long"  and c["high"] >= p["stop"])
                if fired:
                    if d == "short":
                        ae   = p["stop"] * (1 - SLIPPAGE)
                        risk = p["sl"] - ae
                        tp_p = ae - tp_r * risk
                    else:
                        ae   = p["stop"] * (1 + SLIPPAGE)
                        risk = ae - p["sl"]
                        tp_p = ae + tp_r * risk
                    if risk > 0 and 0.001 <= risk / ae <= 0.20:
                        in_trade = True
                        trade = {"ae": ae, "sl": p["sl"], "tp": tp_p,
                                 "risk": risk, "direction": d}
                        triggered.append(p)
                        break
                    triggered.append(p)
            pending = [p for p in pending
                       if p not in triggered and i <= p["expiry"]]

        if in_trade:
            continue
        if day < start or day > end or i < warmup:
            continue

        # Signal suchen (mit angepasstem EMA-Array)
        if i < 2:
            continue
        c0, c1, c2 = candles_all[i], candles_all[i-1], candles_all[i-2]
        e = ema_arr[i]
        if e <= 0:
            continue

        body0 = abs(c0["close"] - c0["open"])
        body1 = abs(c1["close"] - c1["open"])
        body2 = abs(c2["close"] - c2["open"])
        if body0 <= 0:
            continue

        vol0, vol1, vol2 = c0["volume"], c1["volume"], c2["volume"]
        if vol0 <= 0:
            continue

        sig = None
        if direction in ("short", "both"):
            if (c0["close"] > c0["open"] and c1["close"] > c1["open"] and
                    c2["close"] > c2["open"] and
                    body0 < body1 < body2 and vol0 < vol1 < vol2 and
                    c0["close"] > e):
                sl_p = c0["high"]
                stop = c0["low"]
                risk = sl_p - stop
                if 0 < risk / stop <= 0.15:
                    sig = {"direction": "short", "stop": stop, "sl": sl_p}

        if direction in ("long", "both") and sig is None:
            if (c0["close"] < c0["open"] and c1["close"] < c1["open"] and
                    c2["close"] < c2["open"] and
                    body0 < body1 < body2 and vol0 < vol1 < vol2 and
                    c0["close"] < e):
                sl_p = c0["low"]
                stop = c0["high"]
                risk = stop - sl_p
                if 0 < risk / stop <= 0.15:
                    sig = {"direction": "long", "stop": stop, "sl": sl_p}

        if sig:
            pending.append({**sig, "expiry": i + entry_window})

    return results


def kpis_simple(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "pf": 0.0, "p": 1.0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean)**2 for r in r_list) / (n-1)) if n > 1 else 0
    t     = mean / (sd / math.sqrt(n)) if sd > 0 else 0

    def erfc(x):
        t_ = 1 / (1 + 0.3275911 * abs(x))
        p  = t_ * (0.254829592 + t_ * (-0.284496736 + t_ * (1.421413741 +
              t_ * (-1.453152027 + t_ * 1.061405429))))
        return p * math.exp(-x * x)

    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {
        "n": n, "avg_r": round(mean, 3), "wr": round(len(wins)/n, 3),
        "pf": round(gw/gl, 2) if gl > 0 else float("inf"), "p": round(p, 4),
    }


def add_months(date_str: str, months: int) -> str:
    """Einfache Monats-Addition (kein dateutil nötig)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    month = dt.month + months
    year  = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day   = min(dt.day, [31,28,31,30,31,30,31,31,30,31,30,31][month-1])
    return f"{year:04d}-{month:02d}-{day:02d}"


# ─── Haupt-WFA ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--direction", default="short",
                        choices=["short", "long", "both"])
    parser.add_argument("--assets", default=None)
    args = parser.parse_args()

    assets = args.assets.split(",") if args.assets else CANDIDATE_ASSETS
    direction = args.direction

    print(f"\n{'═'*80}")
    print(f"  KDT Walk-Forward Analysis — Phase 1")
    print(f"  IS: {IS_START}→{IS_END}  |  OOS: {OOS_START}→{OOS_END}")
    print(f"  Direction: {direction.upper()}  |  Assets: {assets}")
    print(f"  Grid: {N_COMBINATIONS} Kombinationen  |  Bonferroni α={ALPHA_BONFERRONI:.5f}")
    print(f"{'═'*80}\n")

    # Alle 1H-Candles laden (einmal, dann wiederverwenden)
    all_candles = {}
    for asset in assets:
        raw = load_csv(asset, "15m")
        if raw:
            all_candles[asset] = aggregate_1h(raw)
            print(f"  ✅ {asset}: {len(all_candles[asset])} 1H-Candles geladen")
        else:
            print(f"  ⚠️  {asset}: keine Daten")
    print()

    # ─── Vollständiger IS-Grid-Search ─────────────────────────────────────────
    print(f"  {'─'*76}")
    print(f"  IS GRID-SEARCH ({IS_START} → {IS_END})")
    print(f"  {'─'*76}")
    print(f"  {'EMA':>5} {'Win':>4} {'TP':>5} │ {'n':>4} {'AvgR':>7} {'WR':>6} "
          f"{'PF':>5} {'p':>7} {'Bonf':>6}")
    print(f"  {'─'*76}")

    grid_results = []
    for ema, win, tp in itertools.product(
            GRID["ema_period"], GRID["entry_window"], GRID["tp_r"]):

        all_r = []
        for asset, candles in all_candles.items():
            r = run_kdt_params(candles, IS_START, IS_END, ema, win, tp, direction)
            all_r.extend(r)

        k = kpis_simple(all_r)
        bonf_ok = "✅" if k["p"] < ALPHA_BONFERRONI else ("🟡" if k["p"] < 0.05 else "❌")
        print(f"  {ema:>5} {win:>4} {tp:>5.1f} │ {k['n']:>4} {k['avg_r']:>+7.3f} "
              f"{k['wr']*100:>5.1f}% {k['pf']:>5.2f} {k['p']:>7.4f} {bonf_ok}")
        grid_results.append({"ema": ema, "win": win, "tp": tp, **k})

    # Bester Kandidat
    best = max(grid_results, key=lambda x: x["avg_r"])
    print(f"\n  🏆 BESTER: EMA={best['ema']} Win={best['win']} TP={best['tp']}R "
          f"→ AvgR={best['avg_r']:+.3f}  n={best['n']}  p={best['p']:.4f}")

    # ─── Rolling Walk-Forward ─────────────────────────────────────────────────
    print(f"\n  {'─'*76}")
    print(f"  ROLLING WFA (dynamisch, OOS ≤ {IS_END})")
    print(f"  {'─'*76}")

    fold_results = []
    is_avgs, oos_avgs = [], []

    # Folds so bauen dass OOS immer innerhalb IS_END liegt
    # IS: [fold_start, fold_start+6M]  OOS: [fold_start+6M, fold_start+7M]
    # Erster Fold: IS endet 2025-10-21, OOS bis 2025-11-21
    # Letzter valider Fold: OOS-Ende ≤ IS_END (2026-02-10)
    folds = []
    fold_start = IS_START
    while True:
        fold_is_start  = fold_start
        fold_is_end    = add_months(fold_start, IS_MONTHS)
        fold_oos_start = fold_is_end
        fold_oos_end   = add_months(fold_start, IS_MONTHS + OOS_MONTHS)
        if fold_oos_end > IS_END:
            break
        folds.append((fold_is_start, fold_is_end, fold_oos_start, fold_oos_end))
        fold_start = add_months(fold_start, 1)

    for fold_idx, (fold_is_start, fold_is_end,
                   fold_oos_start, fold_oos_end) in enumerate(folds):
        fold = fold_idx

        # Bestes Grid auf diesem Fold-IS
        fold_grid = []
        for ema, win, tp in itertools.product(
                GRID["ema_period"], GRID["entry_window"], GRID["tp_r"]):
            all_r = []
            for asset, candles in all_candles.items():
                r = run_kdt_params(candles, fold_is_start, fold_is_end,
                                   ema, win, tp, direction)
                all_r.extend(r)
            k = kpis_simple(all_r)
            fold_grid.append({"ema": ema, "win": win, "tp": tp, **k})

        fold_best = max(fold_grid, key=lambda x: x["avg_r"])

        # OOS mit besten Fold-Parametern
        oos_r = []
        for asset, candles in all_candles.items():
            r = run_kdt_params(candles, fold_oos_start, fold_oos_end,
                               fold_best["ema"], fold_best["win"],
                               fold_best["tp"], direction)
            oos_r.extend(r)
        oos_k = kpis_simple(oos_r)

        is_avgs.append(fold_best["avg_r"])
        oos_avgs.append(oos_k["avg_r"])
        oos_pos = "✅" if oos_k["avg_r"] > 0 else "❌"

        print(f"  Fold {fold+1}: IS {fold_is_start}→{fold_is_end} "
              f"OOS {fold_oos_start}→{fold_oos_end}")
        print(f"    IS-Beste : EMA={fold_best['ema']} Win={fold_best['win']} "
              f"TP={fold_best['tp']}R  AvgR={fold_best['avg_r']:+.3f}  n={fold_best['n']}")
        print(f"    OOS      : AvgR={oos_k['avg_r']:+.3f}  n={oos_k['n']}  "
              f"WR={oos_k['wr']*100:.1f}%  {oos_pos}")
        fold_results.append({"fold": fold+1, "is_avg": fold_best["avg_r"],
                              "oos_avg": oos_k["avg_r"], "oos_n": oos_k["n"],
                              "params": fold_best})

    # WFE berechnen
    valid_is  = [x for x in is_avgs if x != 0]
    wfe = (sum(oos_avgs) / len(oos_avgs)) / (sum(valid_is) / len(valid_is)) \
          if valid_is and sum(valid_is) > 0 else 0
    folds_positive = sum(1 for x in oos_avgs if x > 0)
    oos_n_total = sum(r["oos_n"] for r in fold_results)

    print(f"\n  WFE = {wfe:.3f}  (Schwelle: ≥ 0.5)")
    print(f"  Positive Folds: {folds_positive}/{N_FOLDS}  (Schwelle: ≥ 4)")
    print(f"  OOS Trades gesamt: {oos_n_total}")

    # ─── Gate 1 ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  GATE 1 — GO / NO-GO")
    print(f"{'═'*80}")

    g_wfe    = wfe >= 0.5
    n_folds_actual = len(fold_results)
    g_folds  = folds_positive >= max(4, round(n_folds_actual * 0.6))
    g_oos_r  = sum(oos_avgs) / len(oos_avgs) > 0 if oos_avgs else False
    g_oos_n  = oos_n_total >= 15
    g_bonf   = best["p"] < ALPHA_BONFERRONI

    print(f"  {'✅' if g_wfe   else '❌'} WFE ≥ 0.5                    : {wfe:.3f}")
    print(f"  {'✅' if g_folds else '❌'} ≥ 60% Folds positiv          : "
          f"{folds_positive}/{n_folds_actual}")
    print(f"  {'✅' if g_oos_r else '❌'} OOS Avg R > 0       : {sum(oos_avgs)/len(oos_avgs):+.3f}")
    print(f"  {'✅' if g_oos_n else '❌'} OOS n ≥ 15          : {oos_n_total}")
    print(f"  {'✅' if g_bonf  else '🟡'} p < Bonferroni α    : {best['p']:.4f} "
          f"(α={ALPHA_BONFERRONI:.5f})")

    gates = sum([g_wfe, g_folds, g_oos_r, g_oos_n])
    print(f"\n  → {gates}/4 Hard-Gates bestanden")
    if gates >= 3 and g_wfe and g_oos_r:
        print(f"  ✅ GATE 1: GO — Gewinner-Parameter: "
              f"EMA={best['ema']} Win={best['win']} TP={best['tp']}R")
        print(f"  → Weiter zu Phase 2 (Filter-Attribution)")
    else:
        print(f"  ❌ GATE 1: NO-GO — WFA nicht bestanden")
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
