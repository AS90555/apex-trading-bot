#!/usr/bin/env python3
"""
Phase 0.2 — MAE/MFE-Analyse für PDH/PDL-Breakouts.

Für jeden Trade (Breakout-Signal) trackt das Skript:
  - MAE (Max Adverse Excursion)  — tiefster Drawdown vor Exit, in R
  - MFE (Max Favorable Excursion) — höchster Gewinn vor Exit, in R
  - Zeit bis MFE-Peak (Minuten)
  - SL-Hit-Flag, TP-2R-Hit-Flag
  - Final-R bei 12h-Timeout

Exportiert JSONL nach data/analysis/trade_mae_mfe.jsonl (für Phase 0.3/0.4/0.6 wiederverwendbar).
Report: ASCII-Histogramme + Perzentile (25/50/75/90/95) + TP-Empfehlung.

Verwendung:
  python3 scripts/backtest/mae_mfe_analysis.py
  python3 scripts/backtest/mae_mfe_analysis.py --assets ETH,SOL --from 2025-04-21
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import (
    load_csv, aggregate_4h, aggregate_daily, calc_ema,
    EMA_FAST, EMA_SLOW, SL_BUFFER, TAKER_FEE, SLIPPAGE,
)

ANALYSIS_DIR     = os.path.join(PROJECT_DIR, "data", "analysis")
TRADE_EXPORT     = os.path.join(ANALYSIS_DIR, "trade_mae_mfe.jsonl")
TIMEOUT_CANDLES  = 48   # 48 × 15m = 12h
CANDLE_MINUTES   = 15


def track_mae_mfe(direction: str, entry: float, sl: float,
                  future_candles: list[dict]) -> dict:
    """
    Geht durch future_candles und trackt MAE/MFE + SL/TP-Events.
    Kein Early-Exit — wir wollen den vollen Pfad für die Analyse.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return {"mae_r": 0.0, "mfe_r": 0.0, "mfe_time_min": 0,
                "sl_hit": False, "tp_2r_hit": False,
                "final_r": 0.0, "n_candles": 0}

    actual_entry = entry * (1 + SLIPPAGE) if direction == "long" else entry * (1 - SLIPPAGE)
    fee_roundtrip = 2 * actual_entry * TAKER_FEE

    tp_2r = entry + 2 * risk if direction == "long" else entry - 2 * risk

    mae_r = 0.0
    mfe_r = 0.0
    mfe_time_min = 0
    sl_hit = False
    sl_time_min = None
    tp_2r_hit = False
    tp_2r_time_min = None
    final_close = None

    for idx, c in enumerate(future_candles):
        minutes_from_entry = (idx + 1) * CANDLE_MINUTES

        if direction == "long":
            adverse_price  = c["low"]
            favorable_price = c["high"]
            adverse_r = (adverse_price - actual_entry) / risk
            favorable_r = (favorable_price - actual_entry) / risk
        else:
            adverse_price  = c["high"]
            favorable_price = c["low"]
            adverse_r = (actual_entry - adverse_price) / risk
            favorable_r = (actual_entry - favorable_price) / risk

        if adverse_r < mae_r:
            mae_r = adverse_r
        if favorable_r > mfe_r:
            mfe_r = favorable_r
            mfe_time_min = minutes_from_entry

        if not sl_hit:
            if direction == "long" and c["low"] <= sl:
                sl_hit = True
                sl_time_min = minutes_from_entry
            elif direction == "short" and c["high"] >= sl:
                sl_hit = True
                sl_time_min = minutes_from_entry

        if not tp_2r_hit:
            if direction == "long" and c["high"] >= tp_2r:
                tp_2r_hit = True
                tp_2r_time_min = minutes_from_entry
            elif direction == "short" and c["low"] <= tp_2r:
                tp_2r_hit = True
                tp_2r_time_min = minutes_from_entry

        final_close = c["close"]

    if final_close is not None:
        if direction == "long":
            final_r = (final_close - actual_entry) / risk - fee_roundtrip / risk
        else:
            final_r = (actual_entry - final_close) / risk - fee_roundtrip / risk
    else:
        final_r = 0.0

    return {
        "mae_r": round(mae_r, 3),
        "mfe_r": round(mfe_r, 3),
        "mfe_time_min": mfe_time_min,
        "sl_hit": sl_hit,
        "sl_time_min": sl_time_min,
        "tp_2r_hit": tp_2r_hit,
        "tp_2r_time_min": tp_2r_time_min,
        "final_r": round(final_r, 3),
        "n_candles": len(future_candles),
    }


def scan_trades(assets: list[str], start_date: str, end_date: str,
                ema_filter: bool = True, verbose: bool = True) -> list[dict]:
    """
    Identisch zu pdhl_backtest.scan aber trackt MAE/MFE statt Early-Exit.
    """
    trades = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            if verbose:
                print(f"   keine Daten: {asset}")
            continue

        candles_4h = aggregate_4h(candles_15m)
        daily_ohlc = aggregate_daily(candles_15m)

        closes_4h = [c["close"] for c in candles_4h]
        ema_fast  = calc_ema(closes_4h, EMA_FAST)
        ema_slow  = calc_ema(closes_4h, EMA_SLOW)
        ema_by_ts = {candles_4h[i]["time"]: (ema_fast[i], ema_slow[i])
                     for i in range(len(candles_4h))}

        sorted_days = sorted(daily_ohlc.keys())
        traded_today_key = None

        for i, day in enumerate(sorted_days):
            if day < start_date or day > end_date:
                continue
            if i == 0:
                continue

            prev_day = sorted_days[i - 1]
            pdh = daily_ohlc[prev_day]["high"]
            pdl = daily_ohlc[prev_day]["low"]
            if pdh <= pdl:
                continue

            day_dt    = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_start = int(day_dt.timestamp() * 1000)
            day_end   = int((day_dt + timedelta(days=1)).timestamp() * 1000)
            day_candles = [c for c in candles_15m if day_start <= c["time"] < day_end]
            if not day_candles:
                continue

            traded = False
            for j, candle in enumerate(day_candles):
                if traded:
                    break
                price = candle["close"]

                trend = None
                if ema_filter:
                    dt_c = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
                    bucket_h  = (dt_c.hour // 4) * 4
                    bucket_ts = int(datetime(dt_c.year, dt_c.month, dt_c.day,
                                             bucket_h, tzinfo=timezone.utc).timestamp() * 1000)
                    ef, es = ema_by_ts.get(bucket_ts, (None, None))
                    if ef is None or es is None:
                        continue
                    trend = "bull" if ef > es else "bear"

                direction = None
                if price > pdh:
                    if not ema_filter or trend == "bull":
                        direction = "long"
                elif price < pdl:
                    if not ema_filter or trend == "bear":
                        direction = "short"
                if direction is None:
                    continue

                sl = pdl * (1 - SL_BUFFER) if direction == "long" else pdh * (1 + SL_BUFFER)
                risk = abs(price - sl)
                if risk / price < 0.001 or risk / price > 0.15:
                    continue

                future = day_candles[j+1:j+1+TIMEOUT_CANDLES]
                track  = track_mae_mfe(direction, price, sl, future)

                entry_dt = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
                trades.append({
                    "asset":        asset,
                    "day":          day,
                    "entry_ts":     candle["time"],
                    "entry_hour":   entry_dt.hour,
                    "direction":    direction,
                    "entry":        round(price, 6),
                    "sl":           round(sl, 6),
                    "risk_pct":     round(risk / price * 100, 4),
                    "trend":        trend,
                    "mae_r":        track["mae_r"],
                    "mfe_r":        track["mfe_r"],
                    "mfe_time_min": track["mfe_time_min"],
                    "sl_hit":       track["sl_hit"],
                    "sl_time_min":  track["sl_time_min"],
                    "tp_2r_hit":    track["tp_2r_hit"],
                    "tp_2r_time_min": track["tp_2r_time_min"],
                    "final_r":      track["final_r"],
                    "n_candles":    track["n_candles"],
                })
                traded = True

    return trades


# ─── Statistik-Helpers (pure Python) ─────────────────────────────────────────

def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def ascii_histogram(values: list[float], bins: int, low: float, high: float,
                    width: int = 50, label: str = "") -> str:
    if not values:
        return "(keine Werte)"
    bin_width = (high - low) / bins
    counts = [0] * bins
    for v in values:
        if v < low:
            counts[0] += 1
        elif v >= high:
            counts[-1] += 1
        else:
            idx = int((v - low) / bin_width)
            idx = min(idx, bins - 1)
            counts[idx] += 1
    peak = max(counts) or 1
    lines = [f"  {label}"] if label else []
    for i, c in enumerate(counts):
        lo  = low + i * bin_width
        hi  = lo + bin_width
        bar = "█" * int(c / peak * width)
        lines.append(f"    [{lo:+6.2f}..{hi:+6.2f}]  {bar:<{width}}  n={c}")
    return "\n".join(lines)


# ─── Report ──────────────────────────────────────────────────────────────────

def write_trades_jsonl(trades: list[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")


def print_report(trades: list[dict]):
    n = len(trades)
    print()
    print("=" * 70)
    print(f"  MAE/MFE-REPORT — PDH/PDL Baseline (n={n} Trades)")
    print("=" * 70)
    if n == 0:
        print("  Keine Trades gefunden.")
        return

    mfes = [t["mfe_r"] for t in trades]
    maes = [t["mae_r"] for t in trades]
    finals = [t["final_r"] for t in trades]
    mfe_times = [t["mfe_time_min"] for t in trades if t["mfe_time_min"] > 0]

    sl_count = sum(1 for t in trades if t["sl_hit"])
    tp_count = sum(1 for t in trades if t["tp_2r_hit"])

    def ps(vals):
        return (percentile(vals, 0.25), percentile(vals, 0.50),
                percentile(vals, 0.75), percentile(vals, 0.90),
                percentile(vals, 0.95))

    p25_mfe, p50_mfe, p75_mfe, p90_mfe, p95_mfe = ps(mfes)
    p25_mae, p50_mae, p75_mae, p90_mae, p95_mae = ps(maes)

    print(f"\n  === MFE-Perzentile (Max Favorable Excursion in R) ===")
    print(f"  25%:  {p25_mfe:+.3f}R")
    print(f"  50%:  {p50_mfe:+.3f}R   ← Median")
    print(f"  75%:  {p75_mfe:+.3f}R")
    print(f"  90%:  {p90_mfe:+.3f}R")
    print(f"  95%:  {p95_mfe:+.3f}R")
    print(f"  max:  {max(mfes):+.3f}R")

    print(f"\n  === MAE-Perzentile (Max Adverse Excursion in R) ===")
    print(f"  25%:  {p25_mae:+.3f}R")
    print(f"  50%:  {p50_mae:+.3f}R   ← Median")
    print(f"  75%:  {p75_mae:+.3f}R")
    print(f"  90%:  {p90_mae:+.3f}R")
    print(f"  95%:  {p95_mae:+.3f}R")
    print(f"  min:  {min(maes):+.3f}R")

    print(f"\n  === Event-Raten ===")
    print(f"  SL getroffen:     {sl_count:>5}  ({sl_count/n*100:.1f}%)")
    print(f"  TP=2R getroffen:  {tp_count:>5}  ({tp_count/n*100:.1f}%)")
    if mfe_times:
        print(f"  Avg Zeit bis MFE-Peak: {sum(mfe_times)/len(mfe_times):.0f} min "
              f"(Median {percentile(mfe_times, 0.5):.0f} min, "
              f"P90 {percentile(mfe_times, 0.9):.0f} min)")

    print(f"\n  === MFE-Verteilung (Trade erreichte mindestens R-Level?) ===")
    for tp_level in [0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]:
        hits = sum(1 for m in mfes if m >= tp_level)
        pct  = hits / n * 100
        bar  = "█" * int(pct / 2)
        print(f"    MFE ≥ {tp_level:>4.2f}R:  {hits:>5}  ({pct:>5.1f}%)  {bar}")

    print(f"\n  === MFE-Histogramm ===")
    print(ascii_histogram(mfes, bins=12, low=0.0, high=3.0,
                          width=40, label="MFE (0..3R)"))

    print(f"\n  === Final-R (Close bei 12h-Timeout, Fees eingerechnet) ===")
    avg_final = sum(finals) / n
    print(f"  Avg Final-R:  {avg_final:+.3f}R")
    p25_f, p50_f, p75_f, _, _ = ps(finals)
    print(f"  P25:  {p25_f:+.3f}R   P50:  {p50_f:+.3f}R   P75:  {p75_f:+.3f}R")

    print(f"\n  === Hypothetische TP-Empfehlung ===")
    # EV-Rechnung: Annahme SL=-1R, TP=X wird getroffen wenn MFE≥X
    print(f"  (EV = P(MFE≥TP) * TP + P(MAE≤-1R) * (-1) + P(Timeout) * avg_timeout_R)")
    for tp in [0.3, 0.5, 0.75, 1.0, 1.5]:
        p_tp = sum(1 for m in mfes if m >= tp) / n
        p_sl = sum(1 for t in trades if t["mae_r"] <= -1.0) / n
        p_timeout = 1 - p_tp - p_sl
        timeout_r = [t["final_r"] for t in trades
                     if t["mfe_r"] < tp and t["mae_r"] > -1.0]
        avg_timeout = sum(timeout_r) / len(timeout_r) if timeout_r else 0.0
        ev = p_tp * tp + p_sl * (-1.0) + p_timeout * avg_timeout
        flag = "✅" if ev > 0 else ("⚠️" if ev > -0.05 else "❌")
        print(f"  {flag} TP={tp:>4.2f}R:  EV={ev:+.3f}R  "
              f"(hit_rate={p_tp*100:.1f}%, sl_rate={p_sl*100:.1f}%, "
              f"timeout={p_timeout*100:.1f}% @ {avg_timeout:+.3f}R)")

    print(f"\n  Export: {TRADE_EXPORT}")


def main():
    parser = argparse.ArgumentParser(description="MAE/MFE-Analyse für PDH/PDL-Baseline")
    parser.add_argument("--assets", default="ETH,SOL,AVAX,XRP,DOGE,ADA,LINK,SUI,AAVE,BTC")
    parser.add_argument("--from",   dest="start", default="2025-04-21")
    parser.add_argument("--to",     dest="end",   default="2026-04-19")
    parser.add_argument("--no-ema-filter", action="store_true")
    parser.add_argument("--export-only", action="store_true",
                        help="Nur Trades exportieren, keine Report-Ausgabe")
    args = parser.parse_args()

    assets = [a.strip().upper() for a in args.assets.split(",")]
    print(f"🔬 MAE/MFE-Scan: {args.start} → {args.end}")
    print(f"   Assets: {', '.join(assets)}")
    print(f"   EMA-Filter ({EMA_FAST}/{EMA_SLOW} auf 4H): {'AUS' if args.no_ema_filter else 'AN'}")

    trades = scan_trades(assets, args.start, args.end,
                         ema_filter=not args.no_ema_filter, verbose=True)

    print(f"\n   {len(trades)} Trades gescannt")
    write_trades_jsonl(trades, TRADE_EXPORT)

    if not args.export_only:
        print_report(trades)


if __name__ == "__main__":
    main()
