#!/usr/bin/env python3
"""
VAA Phase 4+5 — Asset-Selektion + Robustheit.

Konfiguration : Vol>2.5x / Body<0.6x / TP=3R / F-06 ATR-Expansion  [SHORT-only, 1H]
Phase 4       : Asset-Breakdown, Toxizitäts-Check, finales Universum
Phase 5       : Monte Carlo (5000 Iter.), Parameter-Sensitivität, DSR

Verwendung:
  python3 scripts/backtest/vaa_phase45.py
"""
import math
import os
import random
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE
from scripts.backtest.squeeze_scout  import aggregate_1h
from scripts.backtest.vaa_scout      import build_indicators
from scripts.backtest.vaa_phase3     import (build_extended_indicators,
                                              run_with_filter, FILTERS)

IS_START  = "2025-04-21"
IS_END    = "2026-02-10"
OOS_START = "2026-02-11"
OOS_END   = "2026-04-19"
ALL_START = "2025-04-21"
ALL_END   = "2026-04-19"

DEFAULT_ASSETS = ["BTC","ETH","SOL","AVAX","XRP","DOGE","ADA","LINK","SUI","AAVE"]

VOL_MULT  = 2.5
BODY_MULT = 0.6
TP_R      = 3.0
MC_ITER   = 5000

F06 = FILTERS["F-06 ATR-Expansion"]


# ─── Statistik ────────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n":0,"avg_r":0,"wr":0,"total_r":0,"pf":0,
                "sharpe":0,"max_dd":0,"t":0,"p":1.0,"best":0,"worst":0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean)**2 for r in r_list) / (n-1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1.0 / (1.0 + 0.3275911 * abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p * math.exp(-x * x)
    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {"n":n,"wr":len(wins)/n,"avg_r":mean,"total_r":total,
            "pf":gw/gl if gl>0 else float("inf"),
            "sharpe":mean/sd if sd>0 else 0,
            "max_dd":dd,"t":t,"p":p,
            "best":max(r_list),"worst":min(r_list)}


def equity_curve(r_list: list[float]) -> list[float]:
    curve, cum = [], 0.0
    for r in r_list:
        cum += r; curve.append(cum)
    return curve


def ascii_curve(r_list: list[float], width: int = 40, height: int = 6) -> str:
    if not r_list: return ""
    curve = equity_curve(r_list)
    lo, hi = min(curve), max(curve)
    rng = max(hi - lo, 0.001)
    lines = []
    for row in range(height, 0, -1):
        thresh = lo + rng * row / height
        line = "".join("█" if v >= thresh else " " for v in curve)
        lines.append(f"  |{line[:width]}")
    lines.append(f"  |{'─'*min(len(curve),width)}")
    lines.append(f"  0{'':>{min(len(curve),width)-10}}{curve[-1]:>+.1f}R")
    return "\n".join(lines)


# ─── Monte Carlo ──────────────────────────────────────────────────────────────

def monte_carlo(r_list: list[float], n_iter: int = 5000, seed: int = 42) -> dict:
    rng = random.Random(seed)
    n   = len(r_list)
    if n < 5:
        return {"p5_final": 0, "p95_sharpe": 0, "pct_positive": 0}

    finals, sharpes, max_dds = [], [], []
    for _ in range(n_iter):
        shuffled = r_list[:]
        rng.shuffle(shuffled)
        k = kpis(shuffled)
        finals.append(k["total_r"])
        sharpes.append(k["sharpe"])
        max_dds.append(k["max_dd"])

    finals.sort(); sharpes.sort(); max_dds.sort()
    pct_pos = sum(1 for f in finals if f > 0) / n_iter

    return {
        "p5_final":    finals[int(0.05 * n_iter)],
        "p50_final":   finals[int(0.50 * n_iter)],
        "p95_final":   finals[int(0.95 * n_iter)],
        "p5_sharpe":   sharpes[int(0.05 * n_iter)],
        "p95_sharpe":  sharpes[int(0.95 * n_iter)],
        "p95_max_dd":  max_dds[int(0.95 * n_iter)],
        "pct_positive": pct_pos,
    }


# ─── Deflated Sharpe Ratio ────────────────────────────────────────────────────

def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurt: float = 3.0) -> float:
    """
    López de Prado DSR (vereinfacht).
    Korrigiert Sharpe für multiple Trials und Non-Normalität.
    DSR > 0.5 = echter Edge, < 0.3 = wahrscheinlich Noise.
    """
    if n_obs < 5 or sharpe <= 0:
        return 0.0
    # Sharpe* = Erwarteter Max-Sharpe bei n_trials zufälligen Versuchen
    gamma = 0.5772156649  # Euler-Mascheroni
    sharpe_star = math.sqrt(2 * math.log(n_trials)) - \
                  (math.log(math.log(n_trials)) + math.log(4 * math.pi)) / \
                  (2 * math.sqrt(2 * math.log(n_trials)))
    # Annualisierter Sharpe (1H → ×sqrt(8760))
    sr_annual = sharpe * math.sqrt(8760)
    # DSR = P(SR > SR*) — vereinfachte Normalapprox.
    sr_adj = (sr_annual - sharpe_star) * math.sqrt(n_obs - 1) / \
             math.sqrt(1 - skew * sr_annual + (kurt - 1) / 4 * sr_annual**2) \
             if (1 - skew * sr_annual + (kurt-1)/4 * sr_annual**2) > 0 else 0
    # Normalverteilungs-CDF
    def norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return norm_cdf(sr_adj)


# ─── Parameter-Sensitivität ───────────────────────────────────────────────────

def param_sensitivity(candles_by_asset, inds_by_asset, btc_candles, btc_inds,
                      base_vol, base_body, base_tp) -> list[dict]:
    results = []
    # ±20% um beste Parameter, in 5 Stufen
    vol_range  = [round(base_vol  * f, 2) for f in [0.80, 0.90, 1.00, 1.10, 1.20]]
    body_range = [round(base_body * f, 2) for f in [0.80, 0.90, 1.00, 1.10, 1.20]]
    tp_range   = [round(base_tp   * f, 1) for f in [0.80, 0.90, 1.00, 1.10, 1.20]]

    # Vol-Sensitivität (body+tp fix)
    for vm in vol_range:
        t = run_with_filter(candles_by_asset, inds_by_asset,
                            btc_candles, btc_inds,
                            IS_START, IS_END, vm, base_body, base_tp, F06)
        k = kpis([x["net_r"] for x in t])
        results.append({"param":"vol_mult","value":vm,**k})

    # Body-Sensitivität (vol+tp fix)
    for bm in body_range:
        t = run_with_filter(candles_by_asset, inds_by_asset,
                            btc_candles, btc_inds,
                            IS_START, IS_END, base_vol, bm, base_tp, F06)
        k = kpis([x["net_r"] for x in t])
        results.append({"param":"body_mult","value":bm,**k})

    # TP-Sensitivität (vol+body fix)
    for tp in tp_range:
        t = run_with_filter(candles_by_asset, inds_by_asset,
                            btc_candles, btc_inds,
                            IS_START, IS_END, base_vol, base_body, tp, F06)
        k = kpis([x["net_r"] for x in t])
        results.append({"param":"tp_r","value":tp,**k})

    return results


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("🏗️  VAA Phase 4+5 — Asset-Selektion + Robustheit")
    print(f"   Setup  : Vol>{VOL_MULT}x / Body<{BODY_MULT}x / TP={TP_R}R / F-06 ATR-Expansion")
    print(f"   IS     : {IS_START} → {IS_END}")
    print(f"   OOS    : {OOS_START} → {OOS_END}")
    print()

    # Daten laden
    print("📂 Lade Candles...")
    candles_by_asset = {}
    inds_by_asset    = {}
    for asset in DEFAULT_ASSETS:
        c15 = load_csv(asset, "15m")
        if not c15: continue
        c1h = aggregate_1h(c15)
        candles_by_asset[asset] = c1h
        inds_by_asset[asset]    = build_extended_indicators(c1h)
    btc_candles = candles_by_asset.get("BTC", [])
    btc_inds    = inds_by_asset.get("BTC", [])

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 4 — ASSET-ANALYSE
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'═'*65}")
    print(f"  PHASE 4 — ASSET-ANALYSE (12 Monate, IS+OOS)")
    print(f"{'═'*65}")

    # Vollständige 12-Monate für Asset-Ranking
    all_trades = run_with_filter(candles_by_asset, inds_by_asset,
                                 btc_candles, btc_inds,
                                 ALL_START, ALL_END,
                                 VOL_MULT, BODY_MULT, TP_R, F06)

    print(f"\n  {'Asset':<6} {'n':>4}  {'AvgR':>8}  {'WR':>6}  {'Total R':>8}  "
          f"{'PF':>5}  {'p':>7}  Urteil")
    print(f"  {'─'*70}")

    asset_results = []
    for asset in DEFAULT_ASSETS:
        sub = [t["net_r"] for t in all_trades if t["asset"] == asset]
        if not sub:
            print(f"  {asset:<6} {'0':>4}  {'—':>8}")
            continue
        k   = kpis(sub)
        pf  = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞   "

        # Urteil
        if k["avg_r"] >= 0.30 and k["n"] >= 3:
            verdict = "✅ KEEP"
        elif k["avg_r"] >= 0.0 and k["n"] >= 2:
            verdict = "⚠️  BEOBACHTEN"
        elif k["avg_r"] < -0.10:
            verdict = "❌ TOXISCH"
        else:
            verdict = "— NEUTRAL (zu wenig n)"

        print(f"  {asset:<6} {k['n']:>4}  {k['avg_r']:>+8.4f}  "
              f"{k['wr']*100:>5.1f}%  {k['total_r']:>+8.2f}R  "
              f"{pf:>5}  {k['p']:>7.4f}  {verdict}")
        asset_results.append({"asset": asset, **k, "verdict": verdict})

    # Finales Universum
    keep   = [a for a in asset_results if "KEEP" in a["verdict"]]
    watch  = [a for a in asset_results if "BEOB" in a["verdict"]]
    toxic  = [a for a in asset_results if "TOXISCH" in a["verdict"]]

    print(f"\n  ✅ KEEP     ({len(keep)}):  {', '.join(a['asset'] for a in keep)}")
    print(f"  ⚠️  BEOBACHT ({len(watch)}): {', '.join(a['asset'] for a in watch)}")
    print(f"  ❌ TOXISCH  ({len(toxic)}):  {', '.join(a['asset'] for a in toxic)}")

    final_assets = [a["asset"] for a in keep]
    print(f"\n  → Finales Live-Universum: {', '.join(final_assets)} ({len(final_assets)} Assets)")

    # IS/OOS Split pro Asset
    print(f"\n  IS vs. OOS pro Asset:")
    print(f"  {'Asset':<6} {'IS-AvgR':>8}  {'IS-n':>5}  {'OOS-AvgR':>9}  {'OOS-n':>6}  Stabil?")
    print(f"  {'─'*55}")
    for a in asset_results:
        asset = a["asset"]
        is_sub  = [t["net_r"] for t in all_trades
                   if t["asset"] == asset and t["trigger_day"] <= IS_END]
        oos_sub = [t["net_r"] for t in all_trades
                   if t["asset"] == asset and t["trigger_day"] >= OOS_START]
        is_k  = kpis(is_sub)
        oos_k = kpis(oos_sub)
        stable = "✅" if oos_k["avg_r"] >= 0 and is_k["avg_r"] >= 0 else \
                 "⚠️ " if oos_k["n"] == 0 else "❌"
        oos_str = f"{oos_k['avg_r']:>+9.4f}" if oos_k["n"] > 0 else "       —"
        print(f"  {asset:<6} {is_k['avg_r']:>+8.4f}  {is_k['n']:>5}  "
              f"{oos_str}  {oos_k['n']:>6}  {stable}")

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 5 — ROBUSTHEIT
    # ══════════════════════════════════════════════════════════════════════════
    print(f"\n{'═'*65}")
    print(f"  PHASE 5 — ROBUSTHEIT")
    print(f"{'═'*65}")

    # Komplette IS-Trades für MC/DSR
    is_trades = run_with_filter(candles_by_asset, inds_by_asset,
                                btc_candles, btc_inds,
                                IS_START, IS_END,
                                VOL_MULT, BODY_MULT, TP_R, F06)
    is_rs = [t["net_r"] for t in is_trades]
    ik    = kpis(is_rs)

    print(f"\n  IS-Basis: n={ik['n']}  AvgR={ik['avg_r']:>+.4f}R  "
          f"WR={ik['wr']*100:.0f}%  Sharpe={ik['sharpe']:>+.3f}")

    # 5.1 Monte Carlo
    print(f"\n  ── 5.1 Monte Carlo ({MC_ITER} Iterationen, Trade-Shuffle) ──")
    mc = monte_carlo(is_rs, n_iter=MC_ITER)

    p5_ok   = mc["p5_final"] > 0
    pos_ok  = mc["pct_positive"] >= 0.80
    dd_real = ik["max_dd"]

    print(f"  P5  Final-Equity  : {mc['p5_final']:>+8.2f}R  {'✅' if p5_ok else '❌'} (muss > 0)")
    print(f"  P50 Final-Equity  : {mc['p50_final']:>+8.2f}R")
    print(f"  P95 Final-Equity  : {mc['p95_final']:>+8.2f}R")
    print(f"  Pfade positiv     : {mc['pct_positive']*100:>7.1f}%  {'✅' if pos_ok else '❌'} (muss ≥ 80%)")
    print(f"  P95 Max-DD        : {mc['p95_max_dd']:>8.2f}R  (realisiert: {dd_real:.2f}R)")
    print(f"  Sharpe P5/P95     : {mc['p5_sharpe']:>+7.3f} / {mc['p95_sharpe']:>+7.3f}")

    # Equity Curve
    print(f"\n  Equity-Curve IS (kumulativ):")
    print(ascii_curve(is_rs, width=50))

    # 5.2 Parameter-Sensitivität
    print(f"\n  ── 5.2 Parameter-Sensitivität (±20% um Optimal-Werte) ──")
    sens = param_sensitivity(candles_by_asset, inds_by_asset,
                             btc_candles, btc_inds,
                             VOL_MULT, BODY_MULT, TP_R)

    print(f"\n  Vol-Multiplikator (Body={BODY_MULT}x / TP={TP_R}R fix):")
    print(f"  {'Vol':>6}  {'n':>4}  {'AvgR':>8}  {'WR':>6}  Plateau")
    vol_avgs = [r["avg_r"] for r in sens if r["param"] == "vol_mult"]
    for r in [x for x in sens if x["param"] == "vol_mult"]:
        marker = "◀ BEST" if abs(r["value"] - VOL_MULT) < 0.01 else ""
        print(f"  {r['value']:>6.2f}  {r['n']:>4}  {r['avg_r']:>+8.4f}  "
              f"{r['wr']*100:>5.1f}%  {marker}")

    print(f"\n  Body-Faktor (Vol={VOL_MULT}x / TP={TP_R}R fix):")
    print(f"  {'Body':>6}  {'n':>4}  {'AvgR':>8}  {'WR':>6}  Plateau")
    for r in [x for x in sens if x["param"] == "body_mult"]:
        marker = "◀ BEST" if abs(r["value"] - BODY_MULT) < 0.01 else ""
        print(f"  {r['value']:>6.2f}  {r['n']:>4}  {r['avg_r']:>+8.4f}  "
              f"{r['wr']*100:>5.1f}%  {marker}")

    print(f"\n  Take-Profit (Vol={VOL_MULT}x / Body={BODY_MULT}x fix):")
    print(f"  {'TP':>6}  {'n':>4}  {'AvgR':>8}  {'WR':>6}  Plateau")
    for r in [x for x in sens if x["param"] == "tp_r"]:
        marker = "◀ BEST" if abs(r["value"] - TP_R) < 0.1 else ""
        print(f"  {r['value']:>6.1f}  {r['n']:>4}  {r['avg_r']:>+8.4f}  "
              f"{r['wr']*100:>5.1f}%  {marker}")

    # Plateau-Check
    all_avg_rs = [r["avg_r"] for r in sens]
    plateau_ok = min(all_avg_rs) > -0.10  # kein Parameter liefert tiefrotes Ergebnis
    spike_ok   = max(all_avg_rs) < 3 * ik["avg_r"]  # kein einzelner Spike

    print(f"\n  Plateau-Check:")
    print(f"  Min AvgR über alle Params: {min(all_avg_rs):>+.4f}R  {'✅' if plateau_ok else '❌'}")
    print(f"  Max AvgR (kein Spike)    : {max(all_avg_rs):>+.4f}R  {'✅' if spike_ok else '❌'}")

    # 5.3 Deflated Sharpe Ratio
    print(f"\n  ── 5.3 Deflated Sharpe Ratio (DSR) ──")
    n_trials_total = 27 + 8 + 3  # Grid (27) + Filter-Tests (8) + Phase-3-Varianten (3)
    skew = sum((r - ik["avg_r"])**3 for r in is_rs) / (ik["n"] * (kpis(is_rs)["sharpe"] or 1)**3) \
           if ik["n"] > 2 and ik["sharpe"] != 0 else 0
    kurt = sum((r - ik["avg_r"])**4 for r in is_rs) / (ik["n"] * (ik["avg_r"]**2 + 0.001)**2) \
           if ik["n"] > 2 else 3

    dsr = deflated_sharpe(ik["sharpe"], n_trials=n_trials_total,
                          n_obs=ik["n"], skew=skew, kurt=max(kurt, 1))
    print(f"  IS Sharpe (1H)     : {ik['sharpe']:>+8.4f}")
    print(f"  Anzahl Trials      : {n_trials_total}")
    print(f"  Skew / Kurt        : {skew:>+.3f} / {kurt:>+.3f}")
    print(f"  DSR                : {dsr:>8.4f}  {'✅ (>0.5)' if dsr > 0.5 else '⚠️  (0.3-0.5)' if dsr > 0.3 else '❌ (<0.3)'}")

    # ── HARD GATE Zusammenfassung ─────────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  PHASE-5-HARD-GATE (11 Kriterien)")
    print(f"{'═'*65}")

    oos_trades = run_with_filter(candles_by_asset, inds_by_asset,
                                 btc_candles, btc_inds,
                                 OOS_START, OOS_END,
                                 VOL_MULT, BODY_MULT, TP_R, F06)
    oos_rs = [t["net_r"] for t in oos_trades]
    ok     = kpis(oos_rs)

    from scripts.backtest.vaa_wfa import run_short_only, WFA_FOLDS
    wfa_oos = []
    for is_s, is_e, oos_s, oos_e in WFA_FOLDS:
        ft = run_with_filter(candles_by_asset, inds_by_asset,
                             btc_candles, btc_inds,
                             oos_s, oos_e, VOL_MULT, BODY_MULT, TP_R, F06)
        wfa_oos.append(kpis([t["net_r"] for t in ft])["avg_r"])
    folds_pos = sum(1 for x in wfa_oos if x > 0)
    mean_wfa_is = sum(wfa_oos) / len(wfa_oos)
    wfe = mean_wfa_is / ik["avg_r"] if ik["avg_r"] > 0 else 0

    pf_str = f"{ok['pf']:.2f}" if ok["pf"] != float("inf") else "∞"
    gates = [
        ("IS Avg R > 0",           ik["avg_r"] > 0,              f"{ik['avg_r']:>+.4f}R"),
        ("IS p < 0.05",            ik["p"] < 0.05,               f"p={ik['p']:.4f}"),
        ("OOS Avg R > 0",          ok["avg_r"] > 0,              f"{ok['avg_r']:>+.4f}R"),
        ("OOS n ≥ 8",              ok["n"] >= 8,                 f"n={ok['n']}"),
        ("OOS PF ≥ 1.4",           ok["pf"] >= 1.4,              f"PF={pf_str}"),
        ("WFE ≥ 0.5",              wfe >= 0.5,                   f"WFE={wfe:>.2f}"),
        (f"Folds ≥ 4/6 positiv",   folds_pos >= 4,               f"{folds_pos}/6"),
        ("MC P5 Final-Equity > 0", p5_ok,                        f"{mc['p5_final']:>+.2f}R"),
        ("MC Pfade ≥ 80% positiv", pos_ok,                       f"{mc['pct_positive']*100:.0f}%"),
        ("DSR > 0.3",              dsr > 0.3,                    f"DSR={dsr:.4f}"),
        ("Plateau stabil",         plateau_ok and spike_ok,      "min/max ok"),
    ]

    passed = sum(1 for _, v, _ in gates if v)
    for name, ok_g, val in gates:
        print(f"  {'✅' if ok_g else '❌'}  {name:<30} {val}")

    print(f"\n  Gates bestanden: {passed}/{len(gates)}")

    if passed >= 9:
        print(f"\n  ✅ HARD GATE BESTANDEN — Strategie ist live-fähig!")
        print(f"\n  Finale Strategie:")
        print(f"    Typ       : VAA SHORT-Only")
        print(f"    Timeframe : 1H")
        print(f"    Assets    : {', '.join(final_assets)} (Phase-4-Selektion)")
        print(f"    Trigger   : Vol > {VOL_MULT}×Vol_SMA(50)  +  Body < {BODY_MULT}×Body_SMA(50)")
        print(f"    Filter    : ATR(14) > 1.2 × ATR_SMA(20)  +  Close > EMA(20)")
        print(f"    Entry     : Sell-Stop am Anomalie-Candle-Low (gültig 3 Kerzen)")
        print(f"    SL        : Anomalie-Candle-High")
        print(f"    TP        : {TP_R}R")
        print(f"    Frequenz  : ~3 Trades/Monat über {len(final_assets)} Assets")
        print(f"    OOS Edge  : AvgR={ok['avg_r']:>+.4f}R  WR={ok['wr']*100:.0f}%  PF={pf_str}")
        print(f"\n  Nächster Schritt: Phase 6.1 Code-Migration")
    elif passed >= 7:
        print(f"\n  ⚠️  BEDINGT — {len(gates)-passed} Gate(s) offen, weitere Daten sammeln")
    else:
        print(f"\n  ❌ HARD GATE VERFEHLT — Strategie nicht live-fähig")


if __name__ == "__main__":
    main()
