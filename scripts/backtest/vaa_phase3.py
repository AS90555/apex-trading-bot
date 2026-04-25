#!/usr/bin/env python3
"""
VAA Phase 3 — Filter-Attribution & Frequenz-Optimierung.

Baseline : Vol>3.0x / Body<0.6x / TP=3R  (82 IS-Trades)
Ziel     : Filter die isoliert Delta-AvgR >= +0.05R liefern UND Frequenz erhöhen
           durch Absenkung des Vol-Thresholds auf 2.5x mit Filter-Kompensation.

Getestete Filter (isoliert):
  F-01  Bullish Absorption   : Anomalie-Kerze ist grün (Close > Open) → Käufer aktiv, Verkäufer absorbiert
  F-02  EMA50 Abwärtstrend   : EMA(50) fällt (slope < 0) zum Trigger-Zeitpunkt
  F-03  Upper Wick Dominanz  : Oberer Docht > Kerzenkörper (sichtbare Ablehnung nach oben)
  F-04  Vol-Cluster          : Mindestens 2 der letzten 3 Kerzen haben Vol > 1.5x Vol_SMA
  F-05  RSI Overbought       : RSI(14) > 55 bei SHORT-Trigger (bereits erhitzt)
  F-06  ATR-Expansion        : Aktuelle ATR > 1.2x ATR_SMA(20) (Volatilitäts-Regime)
  F-07  Kein Gap             : |Open - prev_Close| < 0.3x ATR (kein Gap → sauberer Einstieg)
  F-08  BTC Kontext          : BTC-Close < BTC-EMA(50) zum Trigger-Zeitpunkt (Markt bearish)

Verwendung:
  python3 scripts/backtest/vaa_phase3.py
"""
import math
import os
import sys
from datetime import datetime, timezone

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

# Baseline-Parameter (3.0x gibt mehr Trades als 3.5x, noch im Plateau)
BASE_VOL   = 3.0
BASE_BODY  = 0.6
BASE_TP    = 3.0
ENTRY_WINDOW = 3

# Test auch niedrigeren Vol-Threshold mit Filtern
LOW_VOL    = 2.5


# ─── Indikatoren erweitert ────────────────────────────────────────────────────

def _rsi_series(candles: list[dict], period: int = 14) -> list[float]:
    result = [50.0] * len(candles)
    if len(candles) < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, period + 1):
        d = candles[i]["close"] - candles[i-1]["close"]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    result[period] = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100
    for i in range(period + 1, len(candles)):
        d = candles[i]["close"] - candles[i-1]["close"]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        result[i] = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100
    return result


def _ema_series(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i-1] * (1 - k)
    return result


def _atr_series_simple(candles: list[dict], period: int) -> list[float]:
    result = [0.0] * len(candles)
    if len(candles) < period + 1:
        return result
    trs = []
    for i in range(1, period + 1):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    result[period] = sum(trs) / period
    for i in range(period + 1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr = max(h-l, abs(h-pc), abs(l-pc))
        result[i] = (result[i-1] * (period-1) + tr) / period
    return result


def build_extended_indicators(candles: list[dict]) -> list[dict]:
    """Erweiterte Indikatoren: RSI, EMA50, ATR-SMA für Filter."""
    base   = build_indicators(candles)
    closes = [c["close"] for c in candles]
    vols   = [c["volume"] for c in candles]

    ema50   = _ema_series(closes, 50)
    rsi14   = _rsi_series(candles, 14)
    atr14   = _atr_series_simple(candles, 14)
    atr_sma = _ema_series(atr14, 20)  # ATR-SMA(20) als Referenz
    vol_sma = [sum(vols[max(0,i-20):i])/min(i,20) if i > 0 else 0 for i in range(len(candles))]

    result = []
    for i, b in enumerate(base):
        slope_ema50 = ema50[i] - ema50[i-1] if i > 0 and ema50[i-1] > 0 else 0
        result.append({
            **b,
            "ema50":       ema50[i],
            "slope_ema50": slope_ema50,
            "rsi14":       rsi14[i],
            "atr14":       atr14[i],
            "atr_sma20":   atr_sma[i],
            "vol_sma20":   vol_sma[i],
        })
    return result


# ─── Short-Backtest mit optionalem Filter ─────────────────────────────────────

def run_with_filter(candles_by_asset: dict, inds_by_asset: dict,
                    btc_candles: list[dict], btc_inds: list[dict],
                    start: str, end: str,
                    vol_mult: float, body_mult: float, tp_r: float,
                    filter_fn=None) -> list[dict]:
    """
    filter_fn(c, ind, prev_candles, prev_inds, btc_c, btc_ind) → bool
    Gibt True zurück wenn Setup akzeptiert wird.
    """
    trades  = []
    warmup  = 55

    # BTC-Lookup per Timestamp für F-08
    btc_idx_map = {c["time"]: i for i, c in enumerate(btc_candles)} if btc_candles else {}

    for asset, candles in candles_by_asset.items():
        inds     = inds_by_asset[asset]
        btc_inds_list = inds_by_asset.get("BTC", [])
        pending  = []
        in_trade = False
        trade    = {}

        for i, c in enumerate(candles):
            dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")

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

            if pending:
                triggered = []
                for p in pending:
                    if i > p["expiry_idx"] or day < start or day > end:
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
                            "filters":      p.get("filters", {}),
                        }
                        triggered.append(p)
                        break
                pending = [p for p in pending
                           if p not in triggered and i <= p["expiry_idx"]]

            if in_trade:
                continue

            if day < start or day > end or i < warmup:
                continue

            ind = inds[i]
            if ind["vol_sma"] <= 0 or ind["body_sma"] <= 0 or ind["ema20"] <= 0:
                continue

            vol_ratio  = c["volume"] / ind["vol_sma"]
            body_ratio = ind["body"] / ind["body_sma"]

            if vol_ratio <= vol_mult or body_ratio >= body_mult:
                continue
            if c["close"] <= ind["ema20"]:
                continue

            sl = c["high"]
            approx_risk = sl - c["low"]
            if approx_risk <= 0 or approx_risk / c["low"] >= 0.25:
                continue

            # BTC-Kontext für F-08
            btc_i   = btc_idx_map.get(c["time"])
            btc_ind = btc_inds_list[btc_i] if btc_i is not None and btc_i < len(btc_inds_list) else None
            btc_c   = btc_candles[btc_i] if btc_i is not None and btc_i < len(btc_candles) else None

            # Filter anwenden
            if filter_fn is not None:
                if not filter_fn(c, ind, candles[:i], inds[:i], btc_c, btc_ind):
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
            pass

    return trades


# ─── Filter-Definitionen ──────────────────────────────────────────────────────

FILTERS = {
    "F-01 Bullish Absorption": lambda c, ind, pc, pi, bc, bi: c["close"] > c["open"],

    "F-02 EMA50 Abwärtstrend": lambda c, ind, pc, pi, bc, bi:
        ind.get("slope_ema50", 0) < 0,

    "F-03 Upper Wick Dominanz": lambda c, ind, pc, pi, bc, bi:
        (c["high"] - max(c["open"], c["close"])) > abs(c["open"] - c["close"]),

    "F-04 Vol-Cluster": lambda c, ind, pc, pi, bc, bi:
        len(pc) >= 3 and
        sum(1 for j in range(-3, 0)
            if pi[j]["vol_sma"] > 0 and pc[j]["volume"] > 1.5 * pi[j]["vol_sma"]) >= 2,

    "F-05 RSI > 55": lambda c, ind, pc, pi, bc, bi:
        ind.get("rsi14", 50) > 55,

    "F-06 ATR-Expansion": lambda c, ind, pc, pi, bc, bi:
        ind.get("atr_sma20", 0) > 0 and
        ind.get("atr14", 0) > 1.2 * ind.get("atr_sma20", 1),

    "F-07 Kein Gap": lambda c, ind, pc, pi, bc, bi:
        len(pc) >= 1 and ind.get("atr14", 0) > 0 and
        abs(c["open"] - pc[-1]["close"]) < 0.3 * ind.get("atr14", 1),

    "F-08 BTC Bearish": lambda c, ind, pc, pi, bc, bi:
        bi is not None and bi.get("ema50", 0) > 0 and
        bc is not None and bc["close"] < bi.get("ema50", float("inf")),
}


# ─── Statistik ────────────────────────────────────────────────────────────────

def kpis(r_list):
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0, "wr": 0, "pf": 0, "p": 1.0, "total_r": 0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean)**2 for r in r_list) / (n - 1)) if n > 1 else 0
    t     = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1.0 / (1.0 + 0.3275911 * abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p * math.exp(-x * x)
    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {"n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
            "pf": gw/gl if gl > 0 else float("inf"), "p": p, "t": t,
            "sharpe": mean/sd if sd > 0 else 0,
            "max_dd": max((sum(r_list[:j]) - sum(r_list[:k])
                          for j in range(n) for k in range(j)), default=0)}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("🔬 VAA Phase 3 — Filter-Attribution & Frequenz-Optimierung")
    print(f"   Baseline : Vol>{BASE_VOL}x / Body<{BASE_BODY}x / TP={BASE_TP}R  [SHORT-only, 1H]")
    print(f"   IS       : {IS_START} → {IS_END}")
    print(f"   OOS      : {OOS_START} → {OOS_END}")
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

    # ── Baseline ─────────────────────────────────────────────────────────────
    base_trades = run_with_filter(candles_by_asset, inds_by_asset,
                                  btc_candles, btc_inds,
                                  IS_START, IS_END,
                                  BASE_VOL, BASE_BODY, BASE_TP)
    bk = kpis([t["net_r"] for t in base_trades])
    print(f"📊 Baseline (Vol>{BASE_VOL}x): n={bk['n']}  AvgR={bk['avg_r']:>+.4f}R  "
          f"WR={bk['wr']*100:.0f}%  p={bk['p']:.4f}")

    # Auch Low-Vol Baseline (2.5x ohne Filter)
    low_trades = run_with_filter(candles_by_asset, inds_by_asset,
                                 btc_candles, btc_inds,
                                 IS_START, IS_END,
                                 LOW_VOL, BASE_BODY, BASE_TP)
    lk = kpis([t["net_r"] for t in low_trades])
    print(f"📊 Low-Vol  (Vol>{LOW_VOL}x): n={lk['n']}  AvgR={lk['avg_r']:>+.4f}R  "
          f"WR={lk['wr']*100:.0f}%  p={lk['p']:.4f}")
    print()

    # ── Filter-Attribution (auf Low-Vol Basis) ───────────────────────────────
    print(f"🔍 Filter-Attribution (Basis: Vol>{LOW_VOL}x — mehr Setups, Filter kompensiert):")
    print(f"   {'Filter':<28} {'n':>5}  {'AvgR':>8}  {'Delta':>8}  {'WR':>6}  {'PF':>5}  {'p':>7}  Urteil")
    print(f"   {'─'*85}")

    filter_results = []
    for fname, ffn in FILTERS.items():
        try:
            ft = run_with_filter(candles_by_asset, inds_by_asset,
                                 btc_candles, btc_inds,
                                 IS_START, IS_END,
                                 LOW_VOL, BASE_BODY, BASE_TP,
                                 filter_fn=ffn)
            fk = kpis([t["net_r"] for t in ft])
            delta = fk["avg_r"] - lk["avg_r"]
            pf_s  = f"{fk['pf']:.2f}" if fk["pf"] != float("inf") else "∞   "
            skip_rate = 1 - fk["n"] / lk["n"] if lk["n"] > 0 else 0

            # Urteil: hilft der Filter?
            if delta >= 0.05 and fk["p"] < 0.05 and fk["n"] >= 30:
                verdict = "✅ HILFT"
            elif delta >= 0.02 and fk["n"] >= 20:
                verdict = "⚠️  SCHWACH"
            elif delta < -0.05:
                verdict = "❌ SCHADET"
            else:
                verdict = "— NEUTRAL"

            print(f"   {fname:<28} {fk['n']:>5}  {fk['avg_r']:>+8.4f}  {delta:>+8.4f}  "
                  f"{fk['wr']*100:>5.1f}%  {pf_s:>5}  {fk['p']:>7.4f}  {verdict}")
            filter_results.append({
                "name": fname, "n": fk["n"], "avg_r": fk["avg_r"],
                "delta": delta, "p": fk["p"], "fn": ffn,
                "skip_rate": skip_rate
            })
        except Exception as e:
            print(f"   {fname:<28} ERROR: {e}")

    # ── Top-Filter Kombinationen ──────────────────────────────────────────────
    positive_filters = [f for f in filter_results if f["delta"] >= 0.02 and f["n"] >= 15]
    positive_filters.sort(key=lambda x: -x["delta"])

    print(f"\n  Positive Filter: {[f['name'] for f in positive_filters]}")

    if len(positive_filters) >= 2:
        print(f"\n🔗 Kombinationstest (Top-2 Filter):")
        f1, f2 = positive_filters[0], positive_filters[1]

        def combo_fn(c, ind, pc, pi, bc, bi):
            return f1["fn"](c, ind, pc, pi, bc, bi) and f2["fn"](c, ind, pc, pi, bc, bi)

        combo_trades = run_with_filter(candles_by_asset, inds_by_asset,
                                       btc_candles, btc_inds,
                                       IS_START, IS_END,
                                       LOW_VOL, BASE_BODY, BASE_TP,
                                       filter_fn=combo_fn)
        ck = kpis([t["net_r"] for t in combo_trades])
        delta_combo = ck["avg_r"] - lk["avg_r"]
        print(f"   {f1['name']} + {f2['name']}")
        print(f"   n={ck['n']}  AvgR={ck['avg_r']:>+.4f}R  Delta={delta_combo:>+.4f}R  "
              f"WR={ck['wr']*100:.0f}%  p={ck['p']:.4f}")

        # Ist Kombination redundant?
        if ck["avg_r"] > max(f1["avg_r"], f2["avg_r"]) + 0.02:
            print(f"   → Synergie: Kombination stärker als Einzelfilter ✅")
        elif ck["avg_r"] < min(f1["avg_r"], f2["avg_r"]) - 0.05:
            print(f"   → Konflikt: Kombination schwächer, Filter sind redundant ⚠️")
        else:
            print(f"   → Additiv: Kombination ähnlich wie Einzelfilter (kein Synergie-Bonus)")

    # ── Beste Konfiguration: OOS-Test ────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  OOS-TEST: Beste Konfiguration")
    print(f"{'═'*65}")

    # Wähle besten einzelnen Filter (höchster Delta bei n>=20)
    if positive_filters:
        best_f = positive_filters[0]
        print(f"\n  Konfiguration A: Vol>{LOW_VOL}x + {best_f['name']}")
        oos_a = run_with_filter(candles_by_asset, inds_by_asset,
                                btc_candles, btc_inds,
                                OOS_START, OOS_END,
                                LOW_VOL, BASE_BODY, BASE_TP,
                                filter_fn=best_f["fn"])
        ok_a = kpis([t["net_r"] for t in oos_a])
        print(f"  OOS: n={ok_a['n']}  AvgR={ok_a['avg_r']:>+.4f}R  "
              f"WR={ok_a['wr']*100:.0f}%  PF={ok_a['pf']:.2f}  p={ok_a['p']:.4f}")

    # Konfiguration B: Original Vol>3.5x (beste WFA) zum Vergleich
    from scripts.backtest.vaa_wfa import run_short_only
    oos_orig = run_short_only(candles_by_asset, inds_by_asset,
                              OOS_START, OOS_END, 3.5, 0.6, 3.0)
    ok_orig = kpis([t["net_r"] for t in oos_orig])
    print(f"\n  Konfiguration B: Vol>3.5x (Original-Best aus Phase 2)")
    print(f"  OOS: n={ok_orig['n']}  AvgR={ok_orig['avg_r']:>+.4f}R  "
          f"WR={ok_orig['wr']*100:.0f}%  PF={ok_orig['pf']:.2f}  p={ok_orig['p']:.4f}")

    # ── Frequenz-Analyse ─────────────────────────────────────────────────────
    print(f"\n{'─'*65}")
    print(f"  Frequenz-Analyse (Trades pro Monat über 10 Assets):")
    print(f"{'─'*65}")
    months = 10  # IS-Zeitraum

    configs = [
        (f"Vol>3.5x / Body<0.6x (Phase-2-Best)", len(
            run_short_only(candles_by_asset, inds_by_asset,
                           IS_START, IS_END, 3.5, 0.6, 3.0))),
        (f"Vol>3.0x / Body<0.6x (Baseline)", bk["n"]),
        (f"Vol>2.5x / Body<0.6x (Low-Vol)", lk["n"]),
    ]
    if positive_filters:
        f1 = positive_filters[0]
        combo = run_with_filter(candles_by_asset, inds_by_asset,
                                btc_candles, btc_inds,
                                IS_START, IS_END,
                                LOW_VOL, BASE_BODY, BASE_TP,
                                filter_fn=f1["fn"])
        configs.append((f"Vol>2.5x + {f1['name']}", len(combo)))

    for label, n in configs:
        per_month = n / months
        per_asset = n / (months * len(DEFAULT_ASSETS))
        print(f"  {label:<40}: {n:>4} Trades  "
              f"({per_month:.1f}/Monat gesamt, {per_asset:.2f}/Asset/Monat)")

    # ── Empfehlung ────────────────────────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  PHASE-3-EMPFEHLUNG")
    print(f"{'═'*65}")
    if positive_filters:
        best = positive_filters[0]
        print(f"\n  Bester Filter: {best['name']}")
        print(f"  Delta AvgR   : {best['delta']:>+.4f}R auf IS (Basis Vol>2.5x)")
        print(f"  Frequenz     : {best['n']} IS-Trades ({best['n']/months:.1f}/Monat)")
        print()
        if best["delta"] >= 0.05 and best["n"] >= 40:
            print(f"  ✅ PHASE 4 EMPFOHLEN: Asset-Selektion mit Vol>2.5x + {best['name']}")
        elif best["delta"] >= 0.02:
            print(f"  ⚠️  SCHWACHER FILTER — weiteres Grid oder andere Filter testen")
        else:
            print(f"  ❌ KEIN FILTER HILFT — bei Vol>3.5x bleiben (niedrige Frequenz akzeptieren)")
    else:
        print(f"\n  ❌ Kein Filter verbessert Low-Vol-Basis signifikant.")
        print(f"  Empfehlung: Vol>3.5x beibehalten, mehr Live-Daten sammeln.")


if __name__ == "__main__":
    main()
