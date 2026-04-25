#!/usr/bin/env python3
"""
PDH/PDL Breakout Backtest — Previous Day High/Low als echte Marktstruktur-Level.

Strategie:
  Box    = Vortageshoch + Vortagestief (aus 15m-Candles aggregiert)
  Long   = 15m-Close über PDH + EMA21 > EMA55 auf 4H (Trend bullisch)
  Short  = 15m-Close unter PDL + EMA21 < EMA55 auf 4H (Trend bearisch)
  SL     = PDL (Long) / PDH (Short) + 0.1%-Buffer
  TP     = 2R (fix)
  Fee    = 0.06% Taker × 2 + 0.05% Slippage

Verwendung:
  python3 scripts/backtest/pdhl_backtest.py
  python3 scripts/backtest/pdhl_backtest.py --assets ETH,XRP,SOL --from 2026-03-21
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

HIST_DIR    = os.path.join(PROJECT_DIR, "data", "historical")
TAKER_FEE   = 0.0006
SLIPPAGE    = 0.0005
SL_BUFFER   = 0.001   # 0.1% Buffer über/unter SL-Level
EMA_FAST    = 21      # auf 4H
EMA_SLOW    = 55      # auf 4H
MAX_TRADES_PER_DAY = 1  # pro Asset, pro Tag


# ─── Daten-Utilities ──────────────────────────────────────────────────────────

def load_csv(asset: str, interval: str) -> list[dict]:
    path = os.path.join(HIST_DIR, f"{asset}_{interval}.csv")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "time":   int(row["time_ms"]),
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row["volume"]),
            })
    rows.sort(key=lambda x: x["time"])
    return rows


def aggregate_4h(candles_15m: list[dict]) -> list[dict]:
    """Aggregiert 15m-Candles zu 4H-Candles (16 × 15m = 4H)."""
    if not candles_15m:
        return []
    # Gruppiere nach 4H-Bucket (UTC-aligned)
    buckets = {}
    for c in candles_15m:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        bucket_h = (dt.hour // 4) * 4
        bucket_ts = int(datetime(dt.year, dt.month, dt.day, bucket_h, tzinfo=timezone.utc).timestamp() * 1000)
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {"time": bucket_ts, "open": c["open"],
                                   "high": c["high"], "low": c["low"],
                                   "close": c["close"], "volume": c["volume"]}
        else:
            b = buckets[bucket_ts]
            b["high"]   = max(b["high"], c["high"])
            b["low"]    = min(b["low"],  c["low"])
            b["close"]  = c["close"]
            b["volume"] += c["volume"]
    return sorted(buckets.values(), key=lambda x: x["time"])


def aggregate_daily(candles_15m: list[dict]) -> dict:
    """
    Gibt dict von {date_str: {high, low, open, close}} zurück.
    Nutzt UTC-Tage (00:00–23:59 UTC).
    """
    days = {}
    for c in candles_15m:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        if day not in days:
            days[day] = {"open": c["open"], "high": c["high"],
                         "low": c["low"],   "close": c["close"]}
        else:
            d = days[day]
            d["high"]  = max(d["high"], c["high"])
            d["low"]   = min(d["low"],  c["low"])
            d["close"] = c["close"]
    return days


def calc_ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [None] * len(values)
    result = [None] * (period - 1)
    sma = sum(values[:period]) / period
    result.append(sma)
    k = 2 / (period + 1)
    for v in values[period:]:
        result.append(result[-1] * (1 - k) + v * k)
    return result


# ─── Trade-Simulation ─────────────────────────────────────────────────────────

def simulate_trade(direction: str, entry: float, sl: float,
                   future_candles: list[dict]) -> dict:
    """SL-first konservative Simulation. TP = 2R."""
    risk = abs(entry - sl)
    if risk <= 0:
        return {"exit_reason": "invalid_risk", "exit_pnl_r": 0.0}

    tp = entry + 2 * risk if direction == "long" else entry - 2 * risk

    # Fee + Slippage
    actual_entry = entry * (1 + SLIPPAGE) if direction == "long" else entry * (1 - SLIPPAGE)
    fee_entry    = actual_entry * TAKER_FEE
    fee_exit     = actual_entry * TAKER_FEE  # approximiert auf Entry-Notional

    for c in future_candles:
        # SL zuerst (konservativ: schlechtestes Szenario innerhalb der Candle)
        if direction == "long":
            if c["low"] <= sl:
                gross_r = -1.0
                net_pnl = (sl - actual_entry) / risk - (fee_entry + fee_exit) / risk
                return {"exit_reason": "sl", "exit_pnl_r": round(net_pnl, 3)}
            if c["high"] >= tp:
                net_pnl = (tp - actual_entry) / risk - (fee_entry + fee_exit) / risk
                return {"exit_reason": "tp", "exit_pnl_r": round(net_pnl, 3)}
        else:
            if c["high"] >= sl:
                net_pnl = (actual_entry - sl) / risk - (fee_entry + fee_exit) / risk
                return {"exit_reason": "sl", "exit_pnl_r": round(net_pnl, 3)}
            if c["low"] <= tp:
                net_pnl = (actual_entry - tp) / risk - (fee_entry + fee_exit) / risk
                return {"exit_reason": "tp", "exit_pnl_r": round(net_pnl, 3)}

    # Timeout: zum letzten Close liquidieren
    if future_candles:
        last_close = future_candles[-1]["close"]
        if direction == "long":
            net_pnl = (last_close - actual_entry) / risk - (fee_entry + fee_exit) / risk
        else:
            net_pnl = (actual_entry - last_close) / risk - (fee_entry + fee_exit) / risk
        return {"exit_reason": "timeout", "exit_pnl_r": round(net_pnl, 3)}

    return {"exit_reason": "no_data", "exit_pnl_r": 0.0}


# ─── Haupt-Backtest ───────────────────────────────────────────────────────────

def run_pdhl_backtest(assets: list[str], start_date: str, end_date: str,
                      ema_filter: bool = True, verbose: bool = True) -> dict:
    trades  = []
    skips   = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            if verbose:
                print(f"   ⚠️  Keine 15m-Daten für {asset}")
            continue

        candles_4h  = aggregate_4h(candles_15m)
        daily_ohlc  = aggregate_daily(candles_15m)

        # EMA auf 4H berechnen
        closes_4h   = [c["close"] for c in candles_4h]
        ema_fast    = calc_ema(closes_4h, EMA_FAST)
        ema_slow    = calc_ema(closes_4h, EMA_SLOW)
        ema_by_ts   = {candles_4h[i]["time"]: (ema_fast[i], ema_slow[i])
                       for i in range(len(candles_4h))}

        # 15m-Candles als Lookup nach Timestamp
        c15_by_ts   = {c["time"]: c for c in candles_15m}
        c15_sorted  = candles_15m  # bereits sortiert

        # Walk-Forward über alle Tage
        sorted_days = sorted(daily_ohlc.keys())
        traded_today = set()

        for i, day in enumerate(sorted_days):
            if day < start_date or day > end_date:
                continue
            if i == 0:
                continue  # kein Vortag vorhanden

            prev_day = sorted_days[i - 1]
            if prev_day not in daily_ohlc:
                continue

            pdh = daily_ohlc[prev_day]["high"]
            pdl = daily_ohlc[prev_day]["low"]
            day_range = pdh - pdl

            if day_range <= 0:
                continue

            # Tages-Candles (15m) für diesen Tag
            day_dt    = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_start = int(day_dt.timestamp() * 1000)
            day_end   = int((day_dt + timedelta(days=1)).timestamp() * 1000)

            day_candles = [c for c in c15_sorted
                           if day_start <= c["time"] < day_end]

            if not day_candles:
                continue

            traded_today.discard(asset + day)

            for j, candle in enumerate(day_candles):
                if (asset + day) in traded_today:
                    break  # ein Trade pro Asset pro Tag

                price = candle["close"]

                # EMA-Trend-Filter: nächster 4H-Bucket vor diesem Candle
                trend = None
                if ema_filter:
                    # Finde den aktuellen 4H-Bucket
                    dt_c = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
                    bucket_h = (dt_c.hour // 4) * 4
                    bucket_ts = int(datetime(dt_c.year, dt_c.month, dt_c.day,
                                             bucket_h, tzinfo=timezone.utc).timestamp() * 1000)
                    ef, es = ema_by_ts.get(bucket_ts, (None, None))
                    if ef is None or es is None:
                        skips.append({"asset": asset, "day": day, "reason": "no_ema"})
                        continue
                    trend = "bull" if ef > es else "bear"

                # Breakout-Check
                direction = None
                if price > pdh:
                    if not ema_filter or trend == "bull":
                        direction = "long"
                elif price < pdl:
                    if not ema_filter or trend == "bear":
                        direction = "short"

                if direction is None:
                    continue

                # SL berechnen
                if direction == "long":
                    sl = pdl * (1 - SL_BUFFER)
                else:
                    sl = pdh * (1 + SL_BUFFER)

                risk = abs(price - sl)
                if risk / price < 0.001:  # weniger als 0.1% → zu eng
                    skips.append({"asset": asset, "day": day, "reason": "sl_too_tight"})
                    continue
                if risk / price > 0.15:   # mehr als 15% → zu weit
                    skips.append({"asset": asset, "day": day, "reason": "sl_too_wide"})
                    continue

                # Zukünftige Candles (max 48 × 15m = 12h Timeout)
                future = day_candles[j+1:j+49]

                result = simulate_trade(direction, price, sl, future)

                trades.append({
                    "asset":       asset,
                    "day":         day,
                    "direction":   direction,
                    "entry":       round(price, 4),
                    "pdh":         round(pdh, 4),
                    "pdl":         round(pdl, 4),
                    "sl":          round(sl, 4),
                    "trend":       trend,
                    "exit_reason": result["exit_reason"],
                    "exit_pnl_r":  result["exit_pnl_r"],
                })
                traded_today.add(asset + day)

                if verbose:
                    r = result["exit_pnl_r"]
                    icon = "✅" if r > 0 else "❌"
                    print(f"   {icon} {asset:<5} {day} {direction:<5} "
                          f"entry={price:.3f} SL={sl:.3f} → {result['exit_reason']} {r:+.3f}R")

    # Summary
    n = len(trades)
    if n == 0:
        return {"trades": [], "skips": skips, "summary": {"n_trades": 0}}

    wins     = [t for t in trades if t["exit_pnl_r"] > 0]
    total_r  = sum(t["exit_pnl_r"] for t in trades)
    avg_r    = total_r / n
    wr       = len(wins) / n

    gross_win  = sum(t["exit_pnl_r"] for t in wins)
    gross_loss = abs(sum(t["exit_pnl_r"] for t in trades if t["exit_pnl_r"] <= 0))
    pf         = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Max Drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["exit_pnl_r"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # Asset-Breakdown
    asset_stats = {}
    for t in trades:
        a = t["asset"]
        if a not in asset_stats:
            asset_stats[a] = {"n": 0, "total_r": 0.0, "wins": 0}
        asset_stats[a]["n"] += 1
        asset_stats[a]["total_r"] += t["exit_pnl_r"]
        if t["exit_pnl_r"] > 0:
            asset_stats[a]["wins"] += 1

    # Exit-Reason-Breakdown
    exit_stats = {}
    for t in trades:
        r = t["exit_reason"]
        exit_stats[r] = exit_stats.get(r, 0) + 1

    summary = {
        "n_trades": n, "n_skips": len(skips),
        "win_rate": wr, "avg_r": avg_r, "total_r": total_r,
        "profit_factor": pf, "max_drawdown_r": max_dd,
        "asset_stats": asset_stats, "exit_stats": exit_stats,
    }
    return {"trades": trades, "skips": skips, "summary": summary}


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PDH/PDL Breakout Backtest")
    parser.add_argument("--assets", default="ETH,SOL,AVAX,XRP,DOGE,ADA,LINK,SUI,AAVE,BTC")
    parser.add_argument("--from",   dest="start", default="2026-03-22")
    parser.add_argument("--to",     dest="end",   default="2026-04-20")
    parser.add_argument("--no-ema-filter", action="store_true")
    parser.add_argument("--quiet",  action="store_true")
    args = parser.parse_args()

    assets     = [a.strip().upper() for a in args.assets.split(",")]
    ema_filter = not args.no_ema_filter
    verbose    = not args.quiet

    print(f"📊 PDH/PDL Breakout Backtest: {args.start} → {args.end}")
    print(f"   Assets: {', '.join(assets)}")
    print(f"   EMA-Filter ({EMA_FAST}/{EMA_SLOW} auf 4H): {'AN' if ema_filter else 'AUS'}")
    print()

    result  = run_pdhl_backtest(assets, args.start, args.end,
                                ema_filter=ema_filter, verbose=verbose)
    summary = result["summary"]

    print()
    print("=" * 60)
    print("  PDH/PDL BACKTEST ERGEBNIS")
    print("=" * 60)

    if summary["n_trades"] == 0:
        print("  Keine Trades simuliert.")
        return

    print(f"  Trades gesamt:  {summary['n_trades']}")
    print(f"  Skips:          {summary['n_skips']}")
    print(f"  Win-Rate:       {summary['win_rate']*100:.1f}%")
    print(f"  Avg R:          {summary['avg_r']:+.3f}R")
    print(f"  Total R:        {summary['total_r']:+.2f}R")
    print(f"  Profit Factor:  {summary['profit_factor']:.2f}")
    print(f"  Max Drawdown:   {summary['max_drawdown_r']:.2f}R")

    print(f"\n  Exit-Gründe:")
    for reason, count in sorted(summary["exit_stats"].items(), key=lambda x: -x[1]):
        print(f"    {reason:<15} {count:>4}")

    print(f"\n  Asset-Breakdown:")
    print(f"  {'Asset':<7} {'n':>4} {'AvgR':>8} {'WR':>6} {'TotalR':>8}")
    print(f"  {'─'*7} {'─'*4} {'─'*8} {'─'*6} {'─'*8}")
    for asset, st in sorted(summary["asset_stats"].items(),
                             key=lambda x: x[1]["total_r"], reverse=True):
        avg = st["total_r"] / st["n"]
        wr  = st["wins"] / st["n"] * 100
        ok  = "✅" if avg > 0 else ("⚠️ " if avg > -0.1 else "❌")
        print(f"  {ok} {asset:<5} {st['n']:>4} {avg:>+8.3f}R {wr:>5.1f}% {st['total_r']:>+8.2f}R")

    # Vergleich: mit vs. ohne EMA-Filter
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
