#!/usr/bin/env python3
"""
Turtle Soup / Fade-the-Breakout Scout — Phase 2 Nachfolge-Hypothese.

Setup (PDH-Fade = SHORT):
  1. Candle schließt ÜBER PDH  (normaler Long-Breakout-Trigger)
  2. Nachfolgende Candle schließt WIEDER unter PDH  → Failed Breakout
  3. Entry SHORT @ Close der Re-Entry-Candle
  4. SL  = Breakout-High × (1 + SL_BUFFER)
  5. TP  = variiert (0.5R / 1R / 2R / PDL-Target)

Setup (PDL-Fade = LONG): symmetrisch.

Ziel: Prüfen ob ein messbares Signal (Avg R > 0) existiert BEVOR wir
einen vollständigen Optimierungs-Zyklus starten.

Verwendung:
  python3 scripts/backtest/turtle_soup_scout.py
  python3 scripts/backtest/turtle_soup_scout.py --assets ETH,BTC,XRP
"""
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import (
    load_csv, aggregate_daily, TAKER_FEE, SLIPPAGE,
)

DEFAULT_ASSETS  = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
SL_BUFFER       = 0.001   # 0.1% über Breakout-High
TIMEOUT_CANDLES = 48      # 12h max Haltedauer
MIN_RISK_PCT    = 0.001
MAX_RISK_PCT    = 0.15


def fee_adj(r: float, risk: float, entry: float) -> float:
    fee = 2 * entry * TAKER_FEE
    return r - fee / risk


def simulate_trade(direction: str, entry: float, sl: float, tp_r: float,
                   future: list[dict], pdl_target: float = None) -> dict:
    """SL-first Simulation. Gibt r_out und exit_reason zurück."""
    risk = abs(entry - sl)
    if risk <= 0:
        return {"r": 0.0, "reason": "invalid"}

    actual = entry * (1 + SLIPPAGE) if direction == "long" else entry * (1 - SLIPPAGE)
    tp_price = (actual + tp_r * risk) if direction == "long" else (actual - tp_r * risk)

    def sl_hit(c): return (direction == "long" and c["low"] <= sl) or \
                          (direction == "short" and c["high"] >= sl)
    def tp_hit(c): return (direction == "long" and c["high"] >= tp_price) or \
                          (direction == "short" and c["low"] <= tp_price)
    def r_at(p):   return (p - actual) / risk if direction == "long" else (actual - p) / risk

    for c in future:
        if sl_hit(c):
            return {"r": round(fee_adj(-1.0, risk, actual), 4), "reason": "sl"}
        if tp_hit(c):
            return {"r": round(fee_adj(tp_r, risk, actual), 4), "reason": "tp"}
    close = future[-1]["close"] if future else actual
    return {"r": round(fee_adj(r_at(close), risk, actual), 4), "reason": "timeout"}


def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0: return {}
    wins = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw = sum(wins); gl = abs(sum(r for r in r_list if r < 0))
    mean = total / n
    sd = math.sqrt(sum((r - mean)**2 for r in r_list) / (n - 1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    return {
        "n": n, "wr": len(wins) / n, "avg_r": mean, "total_r": total,
        "pf": gw / gl if gl > 0 else float("inf"),
        "sharpe": mean / sd if sd > 0 else 0, "max_dd": dd,
    }


def run_scout(assets: list[str], start: str, end: str) -> list[dict]:
    trades = []
    for asset in assets:
        candles = load_csv(asset, "15m")
        if not candles: continue
        daily = aggregate_daily(candles)
        sorted_days = sorted(daily.keys())
        n_asset = 0

        for i, day in enumerate(sorted_days):
            if day < start or day > end or i == 0: continue
            prev = sorted_days[i - 1]
            pdh = daily[prev]["high"]
            pdl = daily[prev]["low"]
            if pdh <= pdl: continue

            day_dt    = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_start = int(day_dt.timestamp() * 1000)
            day_end   = int((day_dt + timedelta(days=1)).timestamp() * 1000)
            day_c = [c for c in candles if day_start <= c["time"] < day_end]
            if len(day_c) < 2: continue

            traded = False
            for j in range(len(day_c) - 1):
                if traded: break
                trigger = day_c[j]
                confirm = day_c[j + 1]

                # ── PDH-Fade: trigger schließt ÜBER PDH, confirm schließt DARUNTER ──
                if trigger["close"] > pdh and confirm["close"] < pdh:
                    direction = "short"
                    entry  = confirm["close"]
                    sl     = trigger["high"] * (1 + SL_BUFFER)  # über Breakout-High
                    risk_pct = abs(entry - sl) / entry
                    if risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT: continue

                    future = day_c[j + 2 : j + 2 + TIMEOUT_CANDLES]
                    pdl_r  = (entry - pdl) / abs(entry - sl)  # PDL in R

                    rec = {
                        "asset": asset, "day": day, "direction": direction,
                        "entry": round(entry, 6), "sl": round(sl, 6),
                        "pdh": round(pdh, 6), "pdl": round(pdl, 6),
                        "risk_pct": round(risk_pct * 100, 3),
                        "pdl_r": round(pdl_r, 3),
                        "breakout_candle": j,
                    }
                    for tp_r, tag in [(0.5, "tp05r"), (1.0, "tp1r"),
                                      (2.0, "tp2r"), (pdl_r, "tp_pdl")]:
                        res = simulate_trade(direction, entry, sl, tp_r, future)
                        rec[tag] = res["r"]
                        rec[tag + "_reason"] = res["reason"]
                    trades.append(rec)
                    traded = True
                    n_asset += 1
                    continue

                # ── PDL-Fade: trigger schließt UNTER PDL, confirm schließt DARÜBER ──
                if trigger["close"] < pdl and confirm["close"] > pdl:
                    direction = "long"
                    entry  = confirm["close"]
                    sl     = trigger["low"] * (1 - SL_BUFFER)
                    risk_pct = abs(entry - sl) / entry
                    if risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT: continue

                    future = day_c[j + 2 : j + 2 + TIMEOUT_CANDLES]
                    pdh_r  = (pdh - entry) / abs(entry - sl)

                    rec = {
                        "asset": asset, "day": day, "direction": direction,
                        "entry": round(entry, 6), "sl": round(sl, 6),
                        "pdh": round(pdh, 6), "pdl": round(pdl, 6),
                        "risk_pct": round(risk_pct * 100, 3),
                        "pdl_r": round(pdh_r, 3),
                        "breakout_candle": j,
                    }
                    for tp_r, tag in [(0.5, "tp05r"), (1.0, "tp1r"),
                                      (2.0, "tp2r"), (pdh_r, "tp_pdl")]:
                        res = simulate_trade(direction, entry, sl, tp_r, future)
                        rec[tag] = res["r"]
                        rec[tag + "_reason"] = res["reason"]
                    trades.append(rec)
                    traded = True
                    n_asset += 1

        print(f"   {asset:<5}: {n_asset} Turtle-Soup-Setups")
    return trades


def print_results(trades: list[dict]):
    tags = [("tp05r", "TP=0.5R"), ("tp1r", "TP=1R"),
            ("tp2r", "TP=2R"), ("tp_pdl", "TP=PDL")]

    print(f"\n  === Turtle Soup — Gesamt (n={len(trades)}) ===")
    print(f"  {'Strategie':<14} {'WR':>6} {'AvgR':>9} {'TotalR':>10} {'PF':>7} {'Sharpe':>7} {'MaxDD':>7}")
    print(f"  {'-'*14} {'-'*6} {'-'*9} {'-'*10} {'-'*7} {'-'*7} {'-'*7}")
    for tag, label in tags:
        rs = [t[tag] for t in trades]
        k = kpis(rs)
        pf = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
        print(f"  {label:<14} {k['wr']*100:>5.1f}% {k['avg_r']:>+9.4f}R "
              f"{k['total_r']:>+10.2f}R {pf:>7} {k['sharpe']:>+7.3f} {k['max_dd']:>7.2f}R")

    # Richtungs-Split
    for dir_label, direction in [("SHORT (PDH-Fade)", "short"), ("LONG (PDL-Fade)", "long")]:
        sub = [t for t in trades if t["direction"] == direction]
        if not sub: continue
        print(f"\n  --- {dir_label} (n={len(sub)}) ---")
        for tag, label in tags:
            rs = [t[tag] for t in sub]
            k = kpis(rs)
            pf = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
            print(f"  {label:<14} {k['wr']*100:>5.1f}% {k['avg_r']:>+9.4f}R "
                  f"{k['total_r']:>+10.2f}R {pf:>7} {k['sharpe']:>+7.3f}")

    # Cross-Asset für bestes TP
    best_tag = max(tags, key=lambda x: sum(t[x[0]] for t in trades) / len(trades))[0]
    print(f"\n  === Cross-Asset ({best_tag}) ===")
    assets = sorted(set(t["asset"] for t in trades))
    positives = 0
    for asset in assets:
        rs = [t[best_tag] for t in trades if t["asset"] == asset]
        avg = sum(rs) / len(rs)
        icon = "✅" if avg > 0 else "❌"
        if avg > 0: positives += 1
        print(f"  {icon} {asset:<5}: n={len(rs):>3}, AvgR={avg:+.4f}R")
    print(f"  Positive Assets: {positives}/{len(assets)}")

    # Exit-Grund Breakdown (bestes TP)
    print(f"\n  === Exit-Gründe ({best_tag}) ===")
    reasons = {}
    for t in trades:
        r = t.get(best_tag + "_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {r:<10}: {cnt:>4} ({cnt/len(trades)*100:.1f}%)")

    # Schnell-Check: lohnt sich vollständiger Optimierungs-Zyklus?
    rs_best = [t[best_tag] for t in trades]
    k = kpis(rs_best)
    n = k["n"]
    sd = math.sqrt(sum((r - k["avg_r"])**2 for r in rs_best) / (n - 1)) if n > 1 else 1
    t_stat = k["avg_r"] / (sd / math.sqrt(n))
    import math as _m
    def erfc(x):
        t = 1.0 / (1.0 + 0.3275911 * abs(x))
        p = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 +
              t * (-1.453152027 + t * 1.061405429))))
        return p * _m.exp(-x * x)
    p_val = erfc(abs(t_stat) / _m.sqrt(2))

    print(f"\n  === Scout-Entscheidung ===")
    print(f"  Beste Strategie: {best_tag}  Avg R={k['avg_r']:+.4f}R  t={t_stat:+.3f}  p={p_val:.4f}")
    if k["avg_r"] > 0.03 and p_val < 0.05:
        print(f"  ✅ SIGNAL VORHANDEN — vollständiger Optimierungs-Zyklus empfohlen")
    elif k["avg_r"] > 0:
        print(f"  ⚠️  SCHWACHES SIGNAL — Avg R positiv aber p={p_val:.3f} > 0.05")
        print(f"       SHORT-Only oder Asset-Filter testen bevor voller Zyklus startet")
    else:
        print(f"  ❌ KEIN SIGNAL — Projekt-Pause bestätigt, andere Strategie-Familie")


def main():
    parser = argparse.ArgumentParser(description="Turtle Soup Scout")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from", dest="start", default="2025-04-21")
    parser.add_argument("--to",   dest="end",   default="2026-04-19")
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    print(f"🐢 Turtle Soup Scout — Fade-the-Breakout")
    print(f"   Assets: {', '.join(assets)}")
    print(f"   Periode: {args.start} → {args.end}")

    trades = run_scout(assets, args.start, args.end)
    print(f"\n   Gesamt Setups: {len(trades)}")
    if trades:
        print_results(trades)


if __name__ == "__main__":
    main()
