#!/usr/bin/env python3
"""
Weekly High/Low Mean-Reversion Scout.

Testet drei Entry-Varianten gegen Previous Weekly High/Low (PWH/PWL):

  V1 — Sweep-Reject:
       15m-Candle schließt ÜBER PWH → nächste Candle schließt DARUNTER → SHORT
       (Liquiditäts-Sweep + sofortige Ablehnung)

  V2 — Touch-Reject:
       Candle-High berührt PWH (>= PWH) aber Close < PWH → SHORT @ Close
       (Wick-Rejection ohne Schlusskurs-Bestätigung über dem Level)

  V3 — Proximity-Fade:
       Close innerhalb PROXIMITY_PCT% unter PWH → SHORT @ Close
       (Antizipatorischer Fade noch vor dem Touch)

TP-Varianten: 0.5R, 1R, 2R, Weekly-Mid (Mitte zwischen PWH und PWL)
SL: über Candle-High (V1/V2) bzw. über PWH + Buffer (V3)

Verwendung:
  python3 scripts/backtest/weekly_hl_scout.py
  python3 scripts/backtest/weekly_hl_scout.py --assets ETH,BTC,SOL,XRP
"""
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE

DEFAULT_ASSETS  = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
SL_BUFFER       = 0.001   # 0.1% über Candle-High
PROXIMITY_PCT   = 0.002   # 0.2% Nähe an PWH für V3
TIMEOUT_CANDLES = 96      # 24h max (Weekly-Trades brauchen mehr Zeit)
MIN_RISK_PCT    = 0.001
MAX_RISK_PCT    = 0.20


# ─── Wochenaggregation ────────────────────────────────────────────────────────

def aggregate_weekly(candles: list[dict]) -> dict:
    """
    Gibt dict {week_start_str: {high, low, open, close}} zurück.
    Woche = UTC Montag 00:00 bis Sonntag 23:59.
    """
    weeks = {}
    for c in candles:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        # Montag dieser Woche
        monday = dt - timedelta(days=dt.weekday())
        week_key = monday.strftime("%Y-%m-%d")
        if week_key not in weeks:
            weeks[week_key] = {"open": c["open"], "high": c["high"],
                               "low": c["low"],   "close": c["close"]}
        else:
            w = weeks[week_key]
            w["high"]  = max(w["high"], c["high"])
            w["low"]   = min(w["low"],  c["low"])
            w["close"] = c["close"]
    return weeks


def get_week_start(dt: datetime) -> str:
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


# ─── Trade-Simulation ────────────────────────────────────────────────────────

def fee_adj(r: float, risk: float, entry: float) -> float:
    fee = 2 * entry * TAKER_FEE
    return r - fee / risk


def simulate(direction: str, entry: float, sl: float, tp_r: float,
             future: list[dict]) -> dict:
    risk = abs(entry - sl)
    if risk <= 0:
        return {"r": 0.0, "reason": "invalid"}
    actual = entry * (1 + SLIPPAGE) if direction == "long" else entry * (1 - SLIPPAGE)
    tp_p   = (actual + tp_r * risk) if direction == "long" else (actual - tp_r * risk)

    def sl_hit(c): return (direction == "long"  and c["low"]  <= sl) or \
                          (direction == "short" and c["high"] >= sl)
    def tp_hit(c): return (direction == "long"  and c["high"] >= tp_p) or \
                          (direction == "short" and c["low"]  <= tp_p)
    def r_at(p):   return (p - actual) / risk if direction == "long" else (actual - p) / risk

    for c in future:
        if sl_hit(c): return {"r": round(fee_adj(-1.0, risk, actual), 4), "reason": "sl"}
        if tp_hit(c): return {"r": round(fee_adj(tp_r, risk, actual), 4), "reason": "tp"}
    close = future[-1]["close"] if future else actual
    return {"r": round(fee_adj(r_at(close), risk, actual), 4), "reason": "timeout"}


# ─── Haupt-Scan ──────────────────────────────────────────────────────────────

def run_scout(assets: list[str], start: str, end: str) -> dict:
    """Gibt dict {variant: [trade_records]} zurück."""
    results = {"v1_sweep": [], "v2_touch": [], "v3_proximity": []}

    for asset in assets:
        candles = load_csv(asset, "15m")
        if not candles:
            continue

        weekly = aggregate_weekly(candles)
        sorted_weeks = sorted(weekly.keys())

        counts = {"v1": 0, "v2": 0, "v3": 0}

        # Einmalig-pro-Woche-Flag pro Variante und Richtung
        traded_this_week = {"v1": set(), "v2": set(), "v3": set()}

        for idx, candle in enumerate(candles):
            dt  = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")
            if day < start or day > end:
                continue

            week_key  = get_week_start(dt)
            # Previous week
            wk_idx = sorted_weeks.index(week_key) if week_key in sorted_weeks else -1
            if wk_idx < 1:
                continue
            prev_week = sorted_weeks[wk_idx - 1]
            pwh = weekly[prev_week]["high"]
            pwl = weekly[prev_week]["low"]
            week_mid = (pwh + pwl) / 2
            week_range = pwh - pwl
            if week_range <= 0:
                continue

            future = candles[idx + 1: idx + 1 + TIMEOUT_CANDLES]
            if not future:
                continue

            # ── V1: Sweep-Reject (Close > PWH, nächste Candle Close < PWH) ──
            v1_key = f"{week_key}_short"
            if v1_key not in traded_this_week["v1"]:
                if candle["close"] > pwh and future and future[0]["close"] < pwh:
                    entry = future[0]["close"]
                    sl    = candle["high"] * (1 + SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
                        mid_r = (entry - week_mid) / abs(entry - sl)
                        rec = _make_rec(asset, day, week_key, "short", "v1_sweep",
                                        entry, sl, pwh, pwl, week_mid, risk_pct)
                        _add_tps(rec, "short", entry, sl, mid_r, candles[idx+2:idx+2+TIMEOUT_CANDLES])
                        results["v1_sweep"].append(rec)
                        traded_this_week["v1"].add(v1_key)
                        counts["v1"] += 1

            # Symmetrisch: PDL-Sweep Long
            v1_key_l = f"{week_key}_long"
            if v1_key_l not in traded_this_week["v1"]:
                if candle["close"] < pwl and future and future[0]["close"] > pwl:
                    entry = future[0]["close"]
                    sl    = candle["low"] * (1 - SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
                        mid_r = (week_mid - entry) / abs(entry - sl)
                        rec = _make_rec(asset, day, week_key, "long", "v1_sweep",
                                        entry, sl, pwh, pwl, week_mid, risk_pct)
                        _add_tps(rec, "long", entry, sl, mid_r, candles[idx+2:idx+2+TIMEOUT_CANDLES])
                        results["v1_sweep"].append(rec)
                        traded_this_week["v1"].add(v1_key_l)
                        counts["v1"] += 1

            # ── V2: Touch-Reject (Wick >= PWH, Close < PWH) ─────────────────
            v2_key = f"{week_key}_short"
            if v2_key not in traded_this_week["v2"]:
                if candle["high"] >= pwh and candle["close"] < pwh:
                    entry = candle["close"]
                    sl    = candle["high"] * (1 + SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
                        mid_r = (entry - week_mid) / abs(entry - sl)
                        rec = _make_rec(asset, day, week_key, "short", "v2_touch",
                                        entry, sl, pwh, pwl, week_mid, risk_pct)
                        _add_tps(rec, "short", entry, sl, mid_r, future)
                        results["v2_touch"].append(rec)
                        traded_this_week["v2"].add(v2_key)
                        counts["v2"] += 1

            v2_key_l = f"{week_key}_long"
            if v2_key_l not in traded_this_week["v2"]:
                if candle["low"] <= pwl and candle["close"] > pwl:
                    entry = candle["close"]
                    sl    = candle["low"] * (1 - SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
                        mid_r = (week_mid - entry) / abs(entry - sl)
                        rec = _make_rec(asset, day, week_key, "long", "v2_touch",
                                        entry, sl, pwh, pwl, week_mid, risk_pct)
                        _add_tps(rec, "long", entry, sl, mid_r, future)
                        results["v2_touch"].append(rec)
                        traded_this_week["v2"].add(v2_key_l)
                        counts["v2"] += 1

            # ── V3: Proximity-Fade (Close innerhalb PROXIMITY_PCT unter PWH) ─
            v3_key = f"{week_key}_short"
            if v3_key not in traded_this_week["v3"]:
                if candle["close"] < pwh and (pwh - candle["close"]) / pwh <= PROXIMITY_PCT:
                    entry = candle["close"]
                    sl    = pwh * (1 + SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
                        mid_r = (entry - week_mid) / abs(entry - sl)
                        rec = _make_rec(asset, day, week_key, "short", "v3_proximity",
                                        entry, sl, pwh, pwl, week_mid, risk_pct)
                        _add_tps(rec, "short", entry, sl, mid_r, future)
                        results["v3_proximity"].append(rec)
                        traded_this_week["v3"].add(v3_key)
                        counts["v3"] += 1

            v3_key_l = f"{week_key}_long"
            if v3_key_l not in traded_this_week["v3"]:
                if candle["close"] > pwl and (candle["close"] - pwl) / pwl <= PROXIMITY_PCT:
                    entry = candle["close"]
                    sl    = pwl * (1 - SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT:
                        mid_r = (week_mid - entry) / abs(entry - sl)
                        rec = _make_rec(asset, day, week_key, "long", "v3_proximity",
                                        entry, sl, pwh, pwl, week_mid, risk_pct)
                        _add_tps(rec, "long", entry, sl, mid_r, future)
                        results["v3_proximity"].append(rec)
                        traded_this_week["v3"].add(v3_key_l)
                        counts["v3"] += 1

        print(f"   {asset:<5}: V1={counts['v1']:>3}  V2={counts['v2']:>3}  V3={counts['v3']:>3}")

    return results


def _make_rec(asset, day, week_key, direction, variant,
              entry, sl, pwh, pwl, week_mid, risk_pct):
    return {
        "asset": asset, "day": day, "week": week_key,
        "direction": direction, "variant": variant,
        "entry": round(entry, 6), "sl": round(sl, 6),
        "pwh": round(pwh, 6), "pwl": round(pwl, 6),
        "week_mid": round(week_mid, 6),
        "risk_pct": round(risk_pct * 100, 3),
    }


def _add_tps(rec, direction, entry, sl, mid_r, future):
    for tp_r, tag in [(0.5, "tp05r"), (1.0, "tp1r"), (2.0, "tp2r"),
                      (max(mid_r, 0.1), "tp_mid")]:
        res = simulate(direction, entry, sl, tp_r, future)
        rec[tag] = res["r"]
        rec[tag + "_reason"] = res["reason"]


# ─── Auswertung ──────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0: return {"n": 0, "wr": 0, "avg_r": 0, "total_r": 0,
                       "pf": 0, "sharpe": 0, "max_dd": 0}
    wins = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw = sum(wins); gl = abs(sum(r for r in r_list if r < 0))
    mean = total / n
    sd = math.sqrt(sum((r - mean)**2 for r in r_list) / (n - 1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    import math as _m
    def erfc(x):
        t = 1.0 / (1.0 + 0.3275911 * abs(x))
        p = t*(0.254829592+t*(-0.284496736+t*(1.421413741+t*(-1.453152027+t*1.061405429))))
        return p * _m.exp(-x * x)
    p_val = erfc(abs(t_stat) / _m.sqrt(2)) if t_stat != 0 else 1.0
    return {
        "n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
        "pf": gw/gl if gl > 0 else float("inf"),
        "sharpe": mean/sd if sd > 0 else 0,
        "max_dd": dd, "t": t_stat, "p": p_val,
    }


def print_variant(name: str, trades: list[dict]):
    if not trades:
        print(f"\n  {name}: keine Setups")
        return
    tags = [("tp05r", "TP=0.5R"), ("tp1r", "TP=1R"),
            ("tp2r", "TP=2R"), ("tp_mid", "TP=WeekMid")]

    print(f"\n  ═══ {name} (n={len(trades)}) ═══")
    print(f"  {'TP':<12} {'WR':>6} {'AvgR':>9} {'TotalR':>10} {'PF':>6} {'Sharpe':>7} {'t':>7} {'p':>8}")
    print(f"  {'-'*12} {'-'*6} {'-'*9} {'-'*10} {'-'*6} {'-'*7} {'-'*7} {'-'*8}")

    best_tag, best_avg = None, -999
    for tag, label in tags:
        rs = [t[tag] for t in trades]
        k = kpis(rs)
        pf = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
        sig = "✅" if k["p"] < 0.05 and k["avg_r"] > 0 else ("⚠️" if k["p"] < 0.10 and k["avg_r"] > 0 else "")
        print(f"  {label:<12} {k['wr']*100:>5.1f}% {k['avg_r']:>+9.4f}R "
              f"{k['total_r']:>+10.2f}R {pf:>6} {k['sharpe']:>+7.3f} "
              f"{k['t']:>+7.3f} {k['p']:>8.4f} {sig}")
        if k["avg_r"] > best_avg:
            best_avg, best_tag = k["avg_r"], tag

    # Richtungs-Split
    for dir_label, direction in [("SHORT", "short"), ("LONG", "long")]:
        sub = [t for t in trades if t["direction"] == direction]
        if not sub: continue
        rs = [t[best_tag] for t in sub]
        k  = kpis(rs)
        print(f"  → {dir_label} (n={len(sub):>3}): AvgR={k['avg_r']:+.4f}R  WR={k['wr']*100:.1f}%  p={k['p']:.4f}")

    # Cross-Asset (bestes TP)
    assets = sorted(set(t["asset"] for t in trades))
    pos = 0
    asset_line = []
    for asset in assets:
        rs = [t[best_tag] for t in trades if t["asset"] == asset]
        avg = sum(rs)/len(rs) if rs else 0
        if avg > 0: pos += 1
        asset_line.append(f"{asset}:{avg:+.3f}R")
    print(f"  Cross-Asset ({best_tag}): {pos}/{len(assets)} positiv")
    print(f"  {' | '.join(asset_line)}")


def print_summary(results: dict):
    print("\n  ═══ SCOUT-ZUSAMMENFASSUNG ═══")
    for variant, trades in results.items():
        if not trades: continue
        tags = ["tp05r", "tp1r", "tp2r", "tp_mid"]
        best = max(tags, key=lambda t: sum(x[t] for x in trades)/len(trades))
        rs = [t[best] for t in trades]
        k  = kpis(rs)
        verdict = "✅ SIGNAL" if k["avg_r"] > 0.03 and k["p"] < 0.05 \
                  else ("⚠️  SCHWACH" if k["avg_r"] > 0 else "❌ KEIN SIGNAL")
        print(f"  {variant:<16}: n={len(trades):>4}  BestTP={best}  "
              f"AvgR={k['avg_r']:>+8.4f}R  p={k['p']:.4f}  → {verdict}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",   dest="start", default="2025-04-21")
    parser.add_argument("--to",     dest="end",   default="2026-04-19")
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    print(f"📅 Weekly High/Low Mean-Reversion Scout")
    print(f"   Assets:  {', '.join(assets)}")
    print(f"   Periode: {args.start} → {args.end}")
    print(f"   Varianten: V1=Sweep-Reject  V2=Touch-Reject  V3=Proximity-Fade")
    print()

    results = run_scout(assets, args.start, args.end)
    total = sum(len(v) for v in results.values())
    print(f"\n   Gesamt Setups: {total}")

    for name, trades in results.items():
        print_variant(name, trades)

    print_summary(results)


if __name__ == "__main__":
    main()
