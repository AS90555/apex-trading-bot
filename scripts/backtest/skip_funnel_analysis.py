#!/usr/bin/env python3
"""
Phase 0.6 — Skip-Funnel-Analyse für PDH/PDL.

Erfasst ALLE Skip-Kategorien (nicht nur die 3 bestehenden) und simuliert
für jede Kategorie alternative Entry-Regeln, um zu prüfen ob wir profitable
Setups verlieren.

Kategorien:
  - no_ema              → keine EMA (Warmup)
  - sl_too_tight        → risk/price < 0.001 (Alternative: 0.5%-Min-SL)
  - sl_too_wide         → risk/price > 0.15  (Alternative: Cap auf 3%)
  - ema_trend_mismatch  → Breakout, aber EMA-Filter blockt (Alternative: ohne EMA)
  - already_traded      → 2. Breakout am selben Tag (Alternative: mehrere Trades)

Verwendung:
  python3 scripts/backtest/skip_funnel_analysis.py
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import (
    load_csv, aggregate_4h, aggregate_daily, calc_ema, simulate_trade,
    SL_BUFFER, EMA_FAST, EMA_SLOW
)

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]


def scan_with_skip_classification(asset: str, start: str, end: str,
                                  tight_sl_min: float = 0.005,
                                  wide_sl_cap: float  = 0.03) -> dict:
    """
    Gibt dict mit {trades: [...], skips: [...]} zurück.
    Jeder Skip-Eintrag enthält simulated_r wenn alternative Regel anwendbar.
    """
    candles_15m = load_csv(asset, "15m")
    if not candles_15m:
        return {"trades": [], "skips": []}

    candles_4h  = aggregate_4h(candles_15m)
    daily_ohlc  = aggregate_daily(candles_15m)

    closes_4h   = [c["close"] for c in candles_4h]
    ema_fast    = calc_ema(closes_4h, EMA_FAST)
    ema_slow    = calc_ema(closes_4h, EMA_SLOW)
    ema_by_ts   = {candles_4h[i]["time"]: (ema_fast[i], ema_slow[i])
                   for i in range(len(candles_4h))}

    sorted_days = sorted(daily_ohlc.keys())
    trades = []
    skips  = []

    for i, day in enumerate(sorted_days):
        if day < start or day > end: continue
        if i == 0: continue
        prev_day = sorted_days[i - 1]
        pdh = daily_ohlc[prev_day]["high"]
        pdl = daily_ohlc[prev_day]["low"]
        if pdh - pdl <= 0: continue

        day_dt    = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_start = int(day_dt.timestamp() * 1000)
        day_end   = int((day_dt + timedelta(days=1)).timestamp() * 1000)
        day_candles = [c for c in candles_15m if day_start <= c["time"] < day_end]
        if not day_candles: continue

        traded_today = False

        for j, candle in enumerate(day_candles):
            price = candle["close"]

            # EMA-Trend
            dt_c = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
            bucket_h = (dt_c.hour // 4) * 4
            bucket_ts = int(datetime(dt_c.year, dt_c.month, dt_c.day,
                                     bucket_h, tzinfo=timezone.utc).timestamp() * 1000)
            ef, es = ema_by_ts.get(bucket_ts, (None, None))
            if ef is None or es is None:
                # no_ema Skip — aber nur wenn Breakout passiert wäre
                if price > pdh or price < pdl:
                    skips.append({"asset": asset, "day": day, "reason": "no_ema",
                                  "simulated_r": None})
                continue
            trend = "bull" if ef > es else "bear"

            # Breakout-Check
            direction = None
            if price > pdh:
                direction = "long"
            elif price < pdl:
                direction = "short"
            if direction is None:
                continue

            # EMA-Trend-Mismatch?
            aligned = (direction == "long" and trend == "bull") or \
                      (direction == "short" and trend == "bear")
            future = day_candles[j+1:j+49]

            # SL
            if direction == "long":
                sl_default = pdl * (1 - SL_BUFFER)
            else:
                sl_default = pdh * (1 + SL_BUFFER)
            risk_pct = abs(price - sl_default) / price

            # Trade ausführbar?
            if traded_today:
                # Alternative: 2. Breakout am selben Tag
                if risk_pct >= 0.001 and risk_pct <= 0.15 and aligned:
                    result = simulate_trade(direction, price, sl_default, future)
                    skips.append({"asset": asset, "day": day, "reason": "already_traded",
                                  "simulated_r": result["exit_pnl_r"]})
                continue

            if not aligned:
                # Alternative: Trade ohne EMA-Filter
                if risk_pct >= 0.001 and risk_pct <= 0.15:
                    result = simulate_trade(direction, price, sl_default, future)
                    skips.append({"asset": asset, "day": day, "reason": "ema_trend_mismatch",
                                  "simulated_r": result["exit_pnl_r"]})
                continue

            # aligned + kein trade heute
            if risk_pct < 0.001:
                # Alternative: breiteren SL verwenden (tight_sl_min vom Preis)
                if direction == "long":
                    sl_alt = price * (1 - tight_sl_min)
                else:
                    sl_alt = price * (1 + tight_sl_min)
                result = simulate_trade(direction, price, sl_alt, future)
                skips.append({"asset": asset, "day": day, "reason": "sl_too_tight",
                              "simulated_r": result["exit_pnl_r"]})
                continue
            if risk_pct > 0.15:
                # Alternative: engeren SL cappen (wide_sl_cap vom Preis)
                if direction == "long":
                    sl_alt = price * (1 - wide_sl_cap)
                else:
                    sl_alt = price * (1 + wide_sl_cap)
                result = simulate_trade(direction, price, sl_alt, future)
                skips.append({"asset": asset, "day": day, "reason": "sl_too_wide",
                              "simulated_r": result["exit_pnl_r"]})
                continue

            # Regulärer Trade
            result = simulate_trade(direction, price, sl_default, future)
            trades.append({"asset": asset, "day": day, "direction": direction,
                           "exit_pnl_r": result["exit_pnl_r"]})
            traded_today = True

    return {"trades": trades, "skips": skips}


def summarize_skip_category(skips: list, reason: str) -> dict:
    filtered = [s for s in skips if s["reason"] == reason and s.get("simulated_r") is not None]
    no_sim   = [s for s in skips if s["reason"] == reason and s.get("simulated_r") is None]
    if not filtered:
        return {"n": 0, "n_no_sim": len(no_sim), "reason": reason}
    rs = [s["simulated_r"] for s in filtered]
    wins = [r for r in rs if r > 0]
    total = sum(rs)
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = abs(sum(r for r in rs if r < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    return {
        "n":        len(filtered),
        "n_no_sim": len(no_sim),
        "reason":   reason,
        "wr":       len(wins) / len(rs),
        "avg_r":    total / len(rs),
        "total_r":  total,
        "pf":       pf,
    }


def main():
    parser = argparse.ArgumentParser(description="Skip-Funnel-Analyse")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",   dest="start", default="2025-04-21")
    parser.add_argument("--to",     dest="end",   default="2026-04-19")
    parser.add_argument("--tight-sl-min", type=float, default=0.005)
    parser.add_argument("--wide-sl-cap",  type=float, default=0.03)
    args = parser.parse_args()

    assets = [a.strip().upper() for a in args.assets.split(",")]
    print(f"🚦 Skip-Funnel-Analyse")
    print(f"   Assets: {', '.join(assets)}")
    print(f"   Periode: {args.start} → {args.end}")
    print(f"   Tight-SL-Alt:  {args.tight_sl_min*100:.2f}% vom Preis")
    print(f"   Wide-SL-Cap:   {args.wide_sl_cap*100:.2f}% vom Preis")

    all_trades = []
    all_skips  = []
    for asset in assets:
        r = scan_with_skip_classification(asset, args.start, args.end,
                                          args.tight_sl_min, args.wide_sl_cap)
        all_trades.extend(r["trades"])
        all_skips.extend(r["skips"])
        print(f"   {asset:<5}: {len(r['trades']):>4} Trades, {len(r['skips']):>5} Skips")

    print(f"\n  === Gesamt ===")
    print(f"  Trades (Baseline): {len(all_trades)}")
    print(f"  Skips total:       {len(all_skips)}")

    baseline_avg = sum(t["exit_pnl_r"] for t in all_trades) / len(all_trades) if all_trades else 0
    baseline_wr  = sum(1 for t in all_trades if t["exit_pnl_r"] > 0) / len(all_trades) if all_trades else 0
    print(f"  Baseline Avg R:    {baseline_avg:+.4f}R")
    print(f"  Baseline WR:       {baseline_wr*100:.1f}%")

    print(f"\n  === Skip-Kategorien (simulierte Outcomes, falls Regel gelockert würde) ===")
    print(f"  {'Reason':<22} {'n':>6} {'WR':>6} {'AvgR':>9} {'TotalR':>10} {'PF':>7}  Einschätzung")
    print(f"  {'-'*22} {'-'*6} {'-'*6} {'-'*9} {'-'*10} {'-'*7}  {'-'*30}")
    reasons = ["sl_too_tight", "sl_too_wide", "ema_trend_mismatch",
               "already_traded", "no_ema"]
    for reason in reasons:
        s = summarize_skip_category(all_skips, reason)
        if s["n"] == 0 and s.get("n_no_sim", 0) == 0:
            continue
        if s["n"] == 0:
            print(f"  {reason:<22} {'—':>6}  (keine simulierbaren Trades, n_no_sim={s['n_no_sim']})")
            continue
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "∞"
        delta = s["avg_r"] - baseline_avg
        if s["avg_r"] > baseline_avg + 0.02:
            verdict = "🚨 REGEL ZU STRENG — lockern"
        elif s["avg_r"] < baseline_avg - 0.02:
            verdict = "✅ Regel schützt — beibehalten"
        else:
            verdict = "⚖️  neutral"
        print(f"  {reason:<22} {s['n']:>6} {s['wr']*100:>5.1f}% "
              f"{s['avg_r']:>+8.3f}R {s['total_r']:>+9.2f}R {pf_str:>7}  {verdict}")

    # Cross-Check: Pro Asset Skip-Distribution
    print(f"\n  === Skip-Reasons pro Asset ===")
    print(f"  {'Asset':<6}" + "".join(f"{r:>22}" for r in reasons))
    for asset in assets:
        counts = []
        for reason in reasons:
            n = sum(1 for s in all_skips if s["asset"] == asset and s["reason"] == reason)
            counts.append(f"{n:>22}")
        print(f"  {asset:<6}" + "".join(counts))


if __name__ == "__main__":
    main()
