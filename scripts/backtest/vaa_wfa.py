#!/usr/bin/env python3
"""
VAA SHORT-Only Walk-Forward Grid Search.

Grid   : vol_mult (2.5/3.0/3.5) × body_mult (0.4/0.5/0.6) × tp_r (2/3/4) = 27 Kombinationen
IS     : 2025-04-21 → 2026-02-10  (10 Monate)
OOS    : 2026-02-11 → 2026-04-19  (2 Monate)
WFA    : Rolling 6m IS / 1m OOS / Schritt 1m  → 6 Folds
Bonf.  : α_adj = 0.05 / 27 = 0.00185

Verwendung:
  python3 scripts/backtest/vaa_wfa.py
"""
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from itertools import product

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE
from scripts.backtest.squeeze_scout  import aggregate_1h
from scripts.backtest.vaa_scout      import build_indicators

DEFAULT_ASSETS = ["BTC","ETH","SOL","AVAX","XRP","DOGE","ADA","LINK","SUI","AAVE"]

IS_START  = "2025-04-21"
IS_END    = "2026-02-10"
OOS_START = "2026-02-11"
OOS_END   = "2026-04-19"

VOL_MULTS  = [2.5, 3.0, 3.5]
BODY_MULTS = [0.4, 0.5, 0.6]
TP_LEVELS  = [2.0, 3.0, 4.0]
ENTRY_WINDOW = 3
N_TESTS    = len(VOL_MULTS) * len(BODY_MULTS) * len(TP_LEVELS)
BONF_ALPHA = 0.05 / N_TESTS

# WFA Folds: IS=6 Monate, OOS=1 Monat, Schritt=1 Monat
WFA_FOLDS = [
    ("2025-04-21","2025-10-20", "2025-10-21","2025-11-20"),
    ("2025-05-21","2025-11-20", "2025-11-21","2025-12-20"),
    ("2025-06-21","2025-12-20", "2025-12-21","2026-01-20"),
    ("2025-07-21","2026-01-20", "2026-01-21","2026-02-20"),
    ("2025-08-21","2026-02-20", "2026-02-21","2026-03-20"),
    ("2025-09-21","2026-03-20", "2026-03-21","2026-04-19"),
]


# ─── Core Backtest ────────────────────────────────────────────────────────────

def run_short_only(candles_by_asset: dict, inds_by_asset: dict,
                   start: str, end: str,
                   vol_mult: float, body_mult: float, tp_r: float) -> list[dict]:
    """SHORT-Only VAA auf vorberechneten Candles/Indikatoren."""
    trades  = []
    warmup  = 52  # max(50, 50, 20) + 2

    for asset, candles in candles_by_asset.items():
        inds     = inds_by_asset[asset]
        pending  = []
        in_trade = False
        trade    = {}

        for i, c in enumerate(candles):
            dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")

            # ── Offene Position ───────────────────────────────────────────────
            if in_trade:
                ae   = trade["actual_entry"]
                sl   = trade["sl"]
                tp   = trade["tp"]
                risk = trade["risk"]

                hit_sl = c["high"] >= sl
                hit_tp = c["low"]  <= tp

                if hit_sl and not hit_tp:
                    fee_r = (2 * ae * TAKER_FEE) / risk
                    trade.update({"net_r": round(-1.0 - fee_r, 4), "exit_reason": "sl",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; continue

                if hit_tp:
                    fee_r = (2 * ae * TAKER_FEE) / risk
                    trade.update({"net_r": round(tp_r - fee_r, 4), "exit_reason": "tp",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; continue
                continue

            # ── Pending Sell-Stops ────────────────────────────────────────────
            if pending:
                triggered = []
                for p in pending:
                    if i > p["expiry_idx"]:
                        continue
                    if day < start or day > end:
                        continue
                    if c["low"] <= p["stop_price"]:
                        ae   = p["stop_price"] * (1 - SLIPPAGE)
                        sl   = p["sl"]
                        risk = sl - ae
                        if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.25:
                            continue
                        tp = ae - tp_r * risk
                        in_trade = True
                        trade = {
                            "asset":        asset,
                            "direction":    "short",
                            "trigger_day":  p["trigger_day"],
                            "entry_day":    day,
                            "entry_idx":    i,
                            "actual_entry": round(ae, 6),
                            "sl":           round(sl, 6),
                            "tp":           round(tp, 6),
                            "risk":         risk,
                            "vol_ratio":    p["vol_ratio"],
                            "body_ratio":   p["body_ratio"],
                        }
                        triggered.append(p)
                        break
                pending = [p for p in pending
                           if p not in triggered and i <= p["expiry_idx"]]

            if in_trade:
                continue

            # ── Setup-Scan ────────────────────────────────────────────────────
            if day < start or day > end or i < warmup:
                continue

            ind = inds[i]
            if ind["vol_sma"] <= 0 or ind["body_sma"] <= 0 or ind["ema20"] <= 0:
                continue

            vol_ratio  = c["volume"] / ind["vol_sma"]
            body_ratio = ind["body"] / ind["body_sma"]

            if vol_ratio <= vol_mult or body_ratio >= body_mult:
                continue
            if c["close"] <= ind["ema20"]:  # SHORT nur wenn Close > EMA20
                continue

            sl = c["high"]
            approx_risk = sl - c["low"]
            if approx_risk <= 0 or approx_risk / c["low"] >= 0.25:
                continue

            pending.append({
                "stop_price":  c["low"],
                "sl":          round(sl, 6),
                "expiry_idx":  i + ENTRY_WINDOW,
                "trigger_day": day,
                "vol_ratio":   round(vol_ratio, 2),
                "body_ratio":  round(body_ratio, 3),
            })

        if in_trade and "net_r" not in trade:
            pass  # offener Trade verwerfen

    return trades


# ─── Statistik ────────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0, "wr": 0, "total_r": 0,
                "pf": 0, "sharpe": 0, "max_dd": 0, "t": 0, "p": 1.0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean)**2 for r in r_list) / (n - 1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1.0 / (1.0 + 0.3275911 * abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p * math.exp(-x * x)
    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {"n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
            "pf": gw/gl if gl > 0 else float("inf"),
            "sharpe": mean/sd if sd > 0 else 0,
            "max_dd": dd, "t": t, "p": p}


def plateau_score(results: list[dict], vm: float, bm: float, tp: float) -> float:
    """Prüfe ob Nachbar-Parameter ähnlich performen (Plateau-Form)."""
    neighbors = []
    for r in results:
        dv = abs(r["vol_mult"] - vm)
        db = abs(r["body_mult"] - bm)
        dt = abs(r["tp_r"] - tp)
        if dv <= 0.5 and db <= 0.1 and dt <= 1.0 and (dv + db + dt) > 0:
            neighbors.append(r["is_avg_r"])
    if not neighbors:
        return 0.0
    # Wie stabil sind die Nachbarn?
    mean_n = sum(neighbors) / len(neighbors)
    return mean_n  # positiver Wert = stabile Nachbarschaft


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("🔬 VAA SHORT-Only Walk-Forward Grid Search")
    print(f"   Grid    : {len(VOL_MULTS)}×{len(BODY_MULTS)}×{len(TP_LEVELS)} = {N_TESTS} Kombinationen")
    print(f"   IS      : {IS_START} → {IS_END}")
    print(f"   OOS     : {OOS_START} → {OOS_END}")
    print(f"   Bonf. α : {BONF_ALPHA:.5f}  (0.05/{N_TESTS})")
    print(f"   WFA     : {len(WFA_FOLDS)} Folds  (6m IS / 1m OOS)")
    print()

    # ── Daten einmalig laden & aggregieren ───────────────────────────────────
    print("📂 Lade und aggregiere Candles...")
    candles_by_asset = {}
    inds_by_asset    = {}
    for asset in DEFAULT_ASSETS:
        c15 = load_csv(asset, "15m")
        if not c15:
            continue
        c1h = aggregate_1h(c15)
        candles_by_asset[asset] = c1h
        inds_by_asset[asset]    = build_indicators(c1h)
        print(f"   {asset:<5}: {len(c1h)} 1H-Candles")

    # ── Grid Search auf IS ────────────────────────────────────────────────────
    print(f"\n📊 Grid Search (IS: {IS_START} → {IS_END})...")
    print(f"   {'VolMult':>7} {'BodyMult':>8} {'TP':>4}  {'n':>5}  {'AvgR':>8}  {'WR':>6}  {'PF':>6}  {'p':>7}  {'Bonf':>7}")
    print(f"   {'─'*75}")

    all_results = []
    for vm, bm, tp in product(VOL_MULTS, BODY_MULTS, TP_LEVELS):
        trades = run_short_only(candles_by_asset, inds_by_asset,
                                IS_START, IS_END, vm, bm, tp)
        rs = [t["net_r"] for t in trades]
        k  = kpis(rs)
        pf_str = f"{k['pf']:.2f}" if k['pf'] != float("inf") else "∞   "
        bonf   = "✅" if k["p"] < BONF_ALPHA else ("⚠️ " if k["p"] < 0.05 else "❌")
        print(f"   {vm:>7.1f} {bm:>8.1f} {tp:>4.1f}  "
              f"{k['n']:>5}  {k['avg_r']:>+8.4f}  {k['wr']*100:>5.1f}%  "
              f"{pf_str:>6}  {k['p']:>7.4f}  {bonf}")
        all_results.append({
            "vol_mult": vm, "body_mult": bm, "tp_r": tp,
            "is_n": k["n"], "is_avg_r": k["avg_r"], "is_wr": k["wr"],
            "is_pf": k["pf"], "is_p": k["p"], "is_sharpe": k["sharpe"],
        })

    # ── Top-Kandidaten (IS positiv + p < 0.05) ───────────────────────────────
    candidates = [r for r in all_results if r["is_avg_r"] > 0 and r["is_p"] < 0.05]
    candidates.sort(key=lambda x: -x["is_avg_r"])

    print(f"\n  Kandidaten (IS positiv + p<0.05): {len(candidates)}")

    # Plateau-Score hinzufügen
    for c in candidates:
        c["plateau"] = plateau_score(all_results, c["vol_mult"], c["body_mult"], c["tp_r"])

    # Stabile Kandidaten: Plateau > 0
    stable = [c for c in candidates if c["plateau"] > 0]
    stable.sort(key=lambda x: (-x["plateau"], -x["is_avg_r"]))

    # ── WFA auf Top-3-Kandidaten ──────────────────────────────────────────────
    print(f"\n🔄 Walk-Forward Analyse (Top-3 stabilste Kandidaten)...")
    top3 = stable[:3] if stable else candidates[:3]

    for rank, cand in enumerate(top3, 1):
        vm, bm, tp = cand["vol_mult"], cand["body_mult"], cand["tp_r"]
        print(f"\n  Kandidat #{rank}: Vol>{vm}x / Body<{bm}x / TP={tp}R")
        print(f"    IS: AvgR={cand['is_avg_r']:>+.4f}R  WR={cand['is_wr']*100:.0f}%  "
              f"p={cand['is_p']:.4f}  Plateau={cand['plateau']:>+.4f}R")
        print(f"    {'Fold':<6} {'IS-AvgR':>8} {'OOS-AvgR':>9} {'OOS-n':>6} {'WFE':>6}")

        fold_is_avgs  = []
        fold_oos_avgs = []
        folds_positive = 0

        for fold_i, (is_s, is_e, oos_s, oos_e) in enumerate(WFA_FOLDS, 1):
            is_trades  = run_short_only(candles_by_asset, inds_by_asset,
                                        is_s, is_e, vm, bm, tp)
            oos_trades = run_short_only(candles_by_asset, inds_by_asset,
                                        oos_s, oos_e, vm, bm, tp)
            is_k  = kpis([t["net_r"] for t in is_trades])
            oos_k = kpis([t["net_r"] for t in oos_trades])
            wfe   = (oos_k["avg_r"] / is_k["avg_r"]) if is_k["avg_r"] != 0 else 0
            fold_is_avgs.append(is_k["avg_r"])
            fold_oos_avgs.append(oos_k["avg_r"])
            if oos_k["avg_r"] > 0:
                folds_positive += 1
            icon = "✅" if oos_k["avg_r"] > 0 else "❌"
            print(f"    {fold_i:<6} {is_k['avg_r']:>+8.4f} {oos_k['avg_r']:>+9.4f} "
                  f"{oos_k['n']:>6} {wfe:>+6.2f}  {icon}")

        mean_is_avg  = sum(fold_is_avgs)  / len(fold_is_avgs)
        mean_oos_avg = sum(fold_oos_avgs) / len(fold_oos_avgs)
        wfe_total    = mean_oos_avg / mean_is_avg if mean_is_avg != 0 else 0
        print(f"    {'Ø':<6} {mean_is_avg:>+8.4f} {mean_oos_avg:>+9.4f} "
              f"{'':>6} {wfe_total:>+6.2f}  "
              f"({'WFE≥0.5 ✅' if wfe_total >= 0.5 else 'WFE<0.5 ❌'})")
        print(f"    Positive Folds: {folds_positive}/{len(WFA_FOLDS)}")
        cand["wfe"]             = wfe_total
        cand["folds_positive"]  = folds_positive
        cand["mean_oos_avg_r"]  = mean_oos_avg

    # ── OOS-Evaluation des besten Kandidaten ─────────────────────────────────
    best = max(top3, key=lambda x: x.get("mean_oos_avg_r", -99))
    vm, bm, tp = best["vol_mult"], best["body_mult"], best["tp_r"]

    print(f"\n{'═'*65}")
    print(f"  🏆 BESTER KANDIDAT: Vol>{vm}x / Body<{bm}x / TP={tp}R")
    print(f"{'═'*65}")

    oos_trades = run_short_only(candles_by_asset, inds_by_asset,
                                OOS_START, OOS_END, vm, bm, tp)
    oos_rs = [t["net_r"] for t in oos_trades]
    ok = kpis(oos_rs)

    print(f"\n  ── OOS-Report ({OOS_START} → {OOS_END}) ──")
    print(f"  n           : {ok['n']}")
    print(f"  Win-Rate    : {ok['wr']*100:.1f}%")
    print(f"  Avg R       : {ok['avg_r']:>+8.4f}R")
    print(f"  Total R     : {ok['total_r']:>+8.2f}R")
    pf_str = f"{ok['pf']:.2f}" if ok['pf'] != float("inf") else "∞"
    print(f"  Profit Fakt.: {pf_str}")
    print(f"  Sharpe      : {ok['sharpe']:>+8.3f}")
    print(f"  Max DD      : {ok['max_dd']:>8.2f}R")
    print(f"  t / p       : {ok['t']:>+8.3f} / {ok['p']:.4f}")
    print(f"  WFE         : {best.get('wfe', 0):>+8.3f}  {'✅' if best.get('wfe',0) >= 0.5 else '❌'}")
    print(f"  Bonf. α_adj : {BONF_ALPHA:.5f}  {'✅' if ok['p'] < BONF_ALPHA else '❌'}")

    # Cross-Asset OOS
    if oos_trades:
        print(f"\n  Cross-Asset OOS:")
        assets_seen = sorted(set(t["asset"] for t in oos_trades))
        pos = 0
        for asset in assets_seen:
            sub = [t["net_r"] for t in oos_trades if t["asset"] == asset]
            ks  = kpis(sub)
            icon = "✅" if ks["avg_r"] > 0 else "❌"
            if ks["avg_r"] > 0: pos += 1
            print(f"    {icon} {asset:<5}: n={len(sub):>2}  AvgR={ks['avg_r']:>+7.4f}R  WR={ks['wr']*100:.0f}%")
        print(f"  Positive Assets OOS: {pos}/{len(assets_seen)}")

    # ── Gesamtbewertung ───────────────────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  SCOUT-ENTSCHEIDUNG")
    print(f"{'═'*65}")

    gates = {
        "IS Avg R > 0":        best["is_avg_r"] > 0,
        "IS p < 0.05":         best["is_p"] < 0.05,
        "OOS Avg R > 0":       ok["avg_r"] > 0,
        "OOS n ≥ 10":          ok["n"] >= 10,
        "WFE ≥ 0.5":           best.get("wfe", 0) >= 0.5,
        f"Folds ≥ 4/6 positiv": best.get("folds_positive", 0) >= 4,
        "Plateau stabil":      best.get("plateau", 0) > 0,
    }
    passed = sum(1 for v in gates.values() if v)
    for gate, ok_g in gates.items():
        print(f"  {'✅' if ok_g else '❌'}  {gate}")

    print(f"\n  Gates bestanden: {passed}/{len(gates)}")
    if passed >= 6:
        print(f"\n  ✅ SIGNAL BESTÄTIGT — Phase 3 (Filter-Optimierung) empfohlen")
        print(f"     Parameter: Vol>{vm}x / Body<{bm}x / TP={tp}R  [SHORT-only, 1H]")
    elif passed >= 4:
        print(f"\n  ⚠️  SCHWACHES SIGNAL — weitere Tests empfohlen")
        print(f"     Mindestens: IS-Ergebnis sauber, aber OOS zu dünn oder WFE zu niedrig")
    else:
        print(f"\n  ❌ KEIN STABILES SIGNAL — Anti-Pattern dokumentieren")


if __name__ == "__main__":
    main()
