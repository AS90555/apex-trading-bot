#!/usr/bin/env python3
"""
LHU Scout — Liquidation Hunter (Failed Macro Breakout)
=======================================================
Hypothese: Wenn eine 4H-Kerze das 20-Tage-Hoch/-Tief mit Volumen durchbricht
aber darunter/darüber schließt, sind späte Momentum-Trader in einer Liquidations-
Falle. Der Chandelier-Exit fängt die resultierende Panik-Bewegung ein.

Signal-Logik (4H):
  SHORT: High > Highest(High, 120)[exkl.]  UND  Close < Highest(High, 120)[exkl.]
         + Vol > 1.2 × SMA(Vol, 50)
  LONG:  Low < Lowest(Low, 120)[exkl.]    UND  Close > Lowest(Low, 120)[exkl.]
         + Vol > 1.2 × SMA(Vol, 50)

Entry:  Market-Close der Trap-Kerze
Exit:   Chandelier Trailing Stop (2.5×ATR(14), nie gegen den Trade)

Unterschied zu MTR: Wir handeln den FAILED Breakout (Close zurück in Range)
statt den erfolgreichen Breakout (Close außerhalb Range). Turtle-Soup-Prinzip
auf 4H/20-Tage-Makro-Level statt 15m/PDH (wie Turtle Soup PDH: REJECTED).

R-Metrik: (Exit − Entry) / Initial_Risk  wobei Initial_Risk = 2.5×ATR@Entry

Usage:
  venv/bin/python3 scripts/backtest/lhu_scout.py
  venv/bin/python3 scripts/backtest/lhu_scout.py --assets ETH,BTC --dir short
  venv/bin/python3 scripts/backtest/lhu_scout.py --from 2026-01-01
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, aggregate_4h

TAKER_FEE = 0.0006
SLIPPAGE  = 0.0005

# ─── Parameter ────────────────────────────────────────────────────────────────
LOOKBACK   = 120   # 4H-Bars = 20 Tage Makro-Level
ATR_PERIOD = 14
ATR_MULT   = 2.5
VOL_PERIOD = 50
VOL_MULT   = 1.2

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = LOOKBACK + max(ATR_PERIOD, VOL_PERIOD) + 5


# ─── Indikator-Serien ─────────────────────────────────────────────────────────

def _atr_series(candles: list[dict], period: int = ATR_PERIOD) -> list[float]:
    out = [float("nan")] * len(candles)
    atr = None
    for i, c in enumerate(candles):
        tr = c["high"] - c["low"] if i == 0 else max(
            c["high"] - c["low"],
            abs(c["high"] - candles[i-1]["close"]),
            abs(c["low"]  - candles[i-1]["close"]),
        )
        atr = tr if atr is None else (atr * (period - 1) + tr) / period
        if i >= period - 1:
            out[i] = atr
    return out


def _sma_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    buf: list[float] = []
    for i, v in enumerate(values):
        buf.append(v)
        if len(buf) > period: buf.pop(0)
        if len(buf) == period: out[i] = sum(buf) / period
    return out


def _rolling_highest(candles: list[dict], lookback: int) -> list[float]:
    """Highest High der letzten `lookback` Bars EXKLUSIV der aktuellen Bar."""
    out = [float("nan")] * len(candles)
    for i in range(lookback, len(candles)):
        out[i] = max(c["high"] for c in candles[i - lookback:i])
    return out


def _rolling_lowest(candles: list[dict], lookback: int) -> list[float]:
    """Lowest Low der letzten `lookback` Bars EXKLUSIV der aktuellen Bar."""
    out = [float("nan")] * len(candles)
    for i in range(lookback, len(candles)):
        out[i] = min(c["low"] for c in candles[i - lookback:i])
    return out


# ─── KPIs ─────────────────────────────────────────────────────────────────────

def _p_approx(t_abs: float, df: int) -> float:
    if df <= 0: return 1.0
    z = t_abs * (1 - 1 / (4 * df)) / math.sqrt(1 + t_abs**2 / (2 * df))
    return max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))))


def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0, "wr": 0, "pf": 0, "expectancy": 0,
                "total_r": 0, "max_dd": 0, "sharpe": 0, "t": 0, "p": 1.0,
                "p10": 0, "p50": 0, "p90": 0, "max_r": 0, "avg_win": 0, "avg_loss": 0}
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r <= 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_list:
        equity += r
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    if n > 1:
        var    = sum((r - avg_r) ** 2 for r in r_list) / (n - 1)
        std    = math.sqrt(var) if var > 0 else 1e-9
        sharpe = (avg_r / std) * math.sqrt(2190)  # annualisiert 4H-Basis
        t      = avg_r / (std / math.sqrt(n))
    else:
        std, sharpe, t = 0, 0, 0
    p = _p_approx(abs(t), n - 1) if n > 1 else 1.0
    s    = sorted(r_list)
    p10  = s[max(0, int(n * 0.10))]
    p50  = s[int(n * 0.50)]
    p90  = s[min(n-1, int(n * 0.90))]
    avg_win  = sum(wins)   / len(wins)   if wins   else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    return {
        "n": n, "avg_r": round(avg_r, 4), "wr": round(wr, 4),
        "pf": round(pf, 3), "total_r": round(sum(r_list), 2),
        "max_dd": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "t": round(t, 3), "p": round(p, 4),
        "expectancy": round(wr * avg_win + (1 - wr) * avg_loss, 4),
        "avg_win": round(avg_win, 3), "avg_loss": round(avg_loss, 3),
        "p10": round(p10, 2), "p50": round(p50, 2), "p90": round(p90, 2),
        "max_r": round(max(r_list), 2),
    }


def ascii_dist(r_list: list[float], bins: int = 24) -> str:
    if not r_list: return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi: return f"  [{lo:.2f}R] alle gleich"
    width  = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r - lo) / width), bins - 1)] += 1
    max_c = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        bar = "█" * int(c / max_c * 28)
        lines.append(f"  {lo + i*width:+7.2f}R │{bar} ({c})")
    return "\n".join(lines)


# ─── Backtest-Core ────────────────────────────────────────────────────────────

def run_lhu(candles: list[dict], direction: str = "both") -> dict:
    """
    Backtestet LHU auf 4H-Candles.
    Signal: Kerze bricht 20-Tage-Extrem mit Wick, schließt zurück in Range.
    """
    atr_s    = _atr_series(candles, ATR_PERIOD)
    vol_sma  = _sma_series([c["volume"] for c in candles], VOL_PERIOD)
    high_max = _rolling_highest(candles, LOOKBACK)  # exklusiv aktuelle Bar
    low_min  = _rolling_lowest(candles, LOOKBACK)

    longs:  list[float] = []
    shorts: list[float] = []
    trade:  dict | None = None

    for i in range(WARMUP, len(candles)):
        c    = candles[i]
        atr  = atr_s[i]
        vsma = vol_sma[i]
        hmax = high_max[i]
        lmin = low_min[i]

        if math.isnan(atr) or math.isnan(vsma) or math.isnan(hmax):
            continue

        # ── 1. Offenen Trade managen (Chandelier) ─────────────────────────────
        if trade is not None:
            if trade["side"] == "short":
                # Peak = niedrigstes Low seit Entry (Profit-Richtung für Short)
                if c["low"] < trade["peak"]:
                    trade["peak"] = c["low"]
                new_sl = trade["peak"] + ATR_MULT * atr
                trade["sl"] = min(trade["sl"], new_sl)  # SL darf nur sinken
                if c["high"] >= trade["sl"]:
                    exit_p  = trade["sl"]
                    r_gross = (trade["entry"] - exit_p) / trade["initial_risk"]
                    fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    shorts.append(round(r_gross - fees_r, 3))
                    trade = None
            else:  # long
                if c["high"] > trade["peak"]:
                    trade["peak"] = c["high"]
                new_sl = trade["peak"] - ATR_MULT * atr
                trade["sl"] = max(trade["sl"], new_sl)  # SL darf nur steigen
                if c["low"] <= trade["sl"]:
                    exit_p  = trade["sl"]
                    r_gross = (exit_p - trade["entry"]) / trade["initial_risk"]
                    fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    longs.append(round(r_gross - fees_r, 3))
                    trade = None

            if trade is not None:
                continue

        # ── 2. Signal-Erkennung ───────────────────────────────────────────────
        vol_ok = c["volume"] > VOL_MULT * vsma

        # SHORT: Wick über 20-Tage-Hoch, Schlusskurs zurück darunter (Fakeout)
        if direction in ("short", "both") and vol_ok:
            if c["high"] > hmax and c["close"] < hmax:
                entry        = c["close"]
                initial_risk = ATR_MULT * atr
                if initial_risk < 1e-6: continue
                trade = {
                    "side": "short", "entry": entry,
                    "sl": entry + initial_risk,     # SL über Entry (Chandelier-Start)
                    "peak": entry,                  # niedrigstes Low verfolgen
                    "initial_risk": initial_risk,
                }
                continue

        # LONG: Wick unter 20-Tage-Tief, Schlusskurs zurück darüber (Fakeout)
        if direction in ("long", "both") and vol_ok:
            if c["low"] < lmin and c["close"] > lmin:
                entry        = c["close"]
                initial_risk = ATR_MULT * atr
                if initial_risk < 1e-6: continue
                trade = {
                    "side": "long", "entry": entry,
                    "sl": entry - initial_risk,
                    "peak": entry,
                    "initial_risk": initial_risk,
                }

    return {"long": longs, "short": shorts}


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(asset: str, result: dict) -> None:
    for side, r_list in [("SHORT", result["short"]), ("LONG", result["long"])]:
        if not r_list:
            print(f"\n  {asset} {side}  │  n=0  (keine Signale)")
            continue
        k = kpis(r_list)
        print(f"\n{'─'*60}")
        print(f"  {asset} {side}  │  n={k['n']}  AvgR={k['avg_r']:+.4f}R  "
              f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")
        print(f"  Expectancy={k['expectancy']:+.4f}R  "
              f"AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R")
        print(f"  P10={k['p10']:+.2f}R  P50={k['p50']:+.2f}R  "
              f"P90={k['p90']:+.2f}R  Max={k['max_r']:+.2f}R")
        print(ascii_dist(r_list))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(ASSETS))
    parser.add_argument("--dir",    default="both", choices=["long", "short", "both"])
    parser.add_argument("--from",   dest="from_date", default=None)
    args = parser.parse_args()

    assets  = [a.strip().upper() for a in args.assets.split(",")]
    from_ts = None
    if args.from_date:
        from_ts = int(datetime.strptime(args.from_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp() * 1000)

    print(f"LHU Scout — Failed 4H Macro Breakout (Turtle Soup × 20-Tage-Level)")
    print(f"Chandelier: {ATR_MULT}×ATR({ATR_PERIOD}) | Vol: >{VOL_MULT}×SMA({VOL_PERIOD})")
    print(f"Assets: {', '.join(assets)}  |  Richtung: {args.dir.upper()}")

    all_longs:  list[float] = []
    all_shorts: list[float] = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            print(f"\n[SKIP] {asset}: keine 15m-Daten")
            continue
        if from_ts:
            candles_15m = [c for c in candles_15m if c["time"] >= from_ts]
        candles_4h = aggregate_4h(candles_15m)
        if len(candles_4h) < WARMUP + 20:
            print(f"\n[SKIP] {asset}: zu wenig 4H-Bars ({len(candles_4h)})")
            continue

        result = run_lhu(candles_4h, direction=args.dir)
        print(f"\n{'═'*30} {asset} {'═'*30}")
        print_report(asset, result)
        all_longs.extend(result["long"])
        all_shorts.extend(result["short"])

    if len(assets) > 1:
        print(f"\n{'═'*60}")
        print("  PORTFOLIO GESAMT")
        for side, r_list in [("SHORT", all_shorts), ("LONG", all_longs)]:
            if not r_list:
                print(f"\n  {side}  n=0")
                continue
            k = kpis(r_list)
            print(f"\n  {side}  n={k['n']}  AvgR={k['avg_r']:+.4f}R  "
                  f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")
            print(f"  Expectancy={k['expectancy']:+.4f}R  "
                  f"AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R")
            print(f"  P10={k['p10']:+.2f}R  P50={k['p50']:+.2f}R  "
                  f"P90={k['p90']:+.2f}R  Max={k['max_r']:+.2f}R")
            if r_list:
                print(ascii_dist(r_list))
        combined = all_longs + all_shorts
        if combined:
            k = kpis(combined)
            print(f"\n  LONG+SHORT  n={k['n']}  AvgR={k['avg_r']:+.4f}R  "
                  f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")


if __name__ == "__main__":
    main()
