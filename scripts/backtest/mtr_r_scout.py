#!/usr/bin/env python3
"""
MTR-R Scout — Macro Trend Rider + ATR-Regime-Gate
===================================================
Hypothese: MTR-4H-Breakouts (20-Tage-Donchian) nur in Hochvolatilitäts-Regimes
handeln (ATR(14) > 60. Perzentil der letzten 90 Tage = 540 × 4H-Bars).

Begründung: Breakouts in Konsolidierungsphasen sind Fakeouts. Nur wenn der Markt
sich bereits in einem Hochvolatilitäts-Regime befindet, hat ein neues 20-Tage-Hoch
genug institutionelles Momentum für Follow-Through.

Basis: mtr_scout.py (LOOKBACK=120, 4H, Chandelier 2.5×ATR)
Filter: ATR(14) > 60. Perzentil der letzten ATR_REGIME_WINDOW Bars

Usage:
  venv/bin/python3 scripts/backtest/mtr_r_scout.py
  venv/bin/python3 scripts/backtest/mtr_r_scout.py --assets ETH,BTC --dir long
  venv/bin/python3 scripts/backtest/mtr_r_scout.py --percentile 50   # anderen Threshold testen
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
LOOKBACK           = 120   # 4H-Bars = 20 Tage Makro-Level
ATR_PERIOD         = 14
ATR_MULT           = 2.5
VOL_PERIOD         = 50
VOL_MULT           = 1.2
ATR_REGIME_WINDOW  = 540   # 90 Tage × 6 Bars/Tag = 540 × 4H-Bars
ATR_REGIME_PCT     = 60    # Mindest-Perzentil für "Hochvolatilität"

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = LOOKBACK + ATR_REGIME_WINDOW + max(ATR_PERIOD, VOL_PERIOD) + 5


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
    out = [float("nan")] * len(candles)
    for i in range(lookback, len(candles)):
        out[i] = max(c["high"] for c in candles[i - lookback:i])
    return out


def _rolling_lowest(candles: list[dict], lookback: int) -> list[float]:
    out = [float("nan")] * len(candles)
    for i in range(lookback, len(candles)):
        out[i] = min(c["low"] for c in candles[i - lookback:i])
    return out


def _atr_regime_threshold(atr_vals: list[float], window: int,
                           percentile: int) -> list[float]:
    """
    Rolling percentile-Threshold der letzten `window` ATR-Werte.
    Gibt NaN zurück solange das Fenster nicht voll ist.
    Exklusiv der aktuellen Bar (kein Look-Ahead).
    """
    out = [float("nan")] * len(atr_vals)
    buf: list[float] = []
    for i, v in enumerate(atr_vals):
        if not math.isnan(v):
            buf.append(v)
            if len(buf) > window:
                buf.pop(0)
        if len(buf) == window:
            s     = sorted(buf)
            idx   = max(0, int(len(s) * percentile / 100) - 1)
            out[i] = s[idx]
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
        sharpe = (avg_r / std) * math.sqrt(2190)  # annualisiert auf 4H-Basis
        t      = avg_r / (std / math.sqrt(n))
    else:
        std, sharpe, t = 0, 0, 0
    p = _p_approx(abs(t), n - 1) if n > 1 else 1.0
    s   = sorted(r_list)
    p10 = s[max(0, int(n * 0.10))]
    p50 = s[int(n * 0.50)]
    p90 = s[min(n-1, int(n * 0.90))]
    avg_win  = sum(wins)   / len(wins)   if wins   else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = wr * avg_win + (1 - wr) * avg_loss
    return {
        "n": n, "avg_r": round(avg_r, 4), "wr": round(wr, 4),
        "pf": round(pf, 3), "total_r": round(sum(r_list), 2),
        "max_dd": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "t": round(t, 3), "p": round(p, 4),
        "expectancy": round(expectancy, 4),
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

def run_mtr_r(candles: list[dict], direction: str = "both",
              regime_pct: int = ATR_REGIME_PCT) -> dict:
    atr_s      = _atr_series(candles, ATR_PERIOD)
    vol_sma    = _sma_series([c["volume"] for c in candles], VOL_PERIOD)
    high_max   = _rolling_highest(candles, LOOKBACK)
    low_min    = _rolling_lowest(candles, LOOKBACK)
    regime_thr = _atr_regime_threshold(atr_s, ATR_REGIME_WINDOW, regime_pct)

    longs:  list[float] = []
    shorts: list[float] = []
    trade:  dict | None = None
    skips_regime = 0
    signals_total = 0

    for i in range(WARMUP, len(candles)):
        c    = candles[i]
        atr  = atr_s[i]
        vsma = vol_sma[i]
        hmax = high_max[i]
        lmin = low_min[i]
        rthr = regime_thr[i]

        if math.isnan(atr) or math.isnan(vsma) or math.isnan(hmax) or math.isnan(rthr):
            continue

        # ── 1. Offenen Trade managen ──────────────────────────────────────────
        if trade is not None:
            if trade["side"] == "long":
                if c["high"] > trade["peak"]:
                    trade["peak"] = c["high"]
                new_sl = trade["peak"] - ATR_MULT * atr
                trade["sl"] = max(trade["sl"], new_sl)
                if c["low"] <= trade["sl"]:
                    exit_p  = trade["sl"]
                    r_gross = (exit_p - trade["entry"]) / trade["initial_risk"]
                    fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    longs.append(round(r_gross - fees_r, 3))
                    trade = None
            else:
                if c["low"] < trade["peak"]:
                    trade["peak"] = c["low"]
                new_sl = trade["peak"] + ATR_MULT * atr
                trade["sl"] = min(trade["sl"], new_sl)
                if c["high"] >= trade["sl"]:
                    exit_p  = trade["sl"]
                    r_gross = (trade["entry"] - exit_p) / trade["initial_risk"]
                    fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    shorts.append(round(r_gross - fees_r, 3))
                    trade = None

            if trade is not None:
                continue

        # ── 2. Signal-Erkennung ───────────────────────────────────────────────
        vol_ok = c["volume"] > VOL_MULT * vsma

        if direction in ("long", "both") and vol_ok and c["close"] > hmax:
            signals_total += 1
            if atr <= rthr:  # Regime-Gate: nicht im Hochvol-Regime
                skips_regime += 1
                continue
            entry        = c["close"]
            initial_risk = ATR_MULT * atr
            if initial_risk < 1e-6: continue
            trade = {"side": "long", "entry": entry, "sl": entry - initial_risk,
                     "peak": entry, "initial_risk": initial_risk}
            continue

        if direction in ("short", "both") and vol_ok and c["close"] < lmin:
            signals_total += 1
            if atr <= rthr:
                skips_regime += 1
                continue
            entry        = c["close"]
            initial_risk = ATR_MULT * atr
            if initial_risk < 1e-6: continue
            trade = {"side": "short", "entry": entry, "sl": entry + initial_risk,
                     "peak": entry, "initial_risk": initial_risk}

    return {"long": longs, "short": shorts,
            "signals_total": signals_total, "skips_regime": skips_regime}


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(asset: str, result: dict) -> None:
    skip_rate = (result["skips_regime"] / result["signals_total"] * 100
                 if result["signals_total"] else 0)
    print(f"\n  Regime-Filter: {result['skips_regime']}/{result['signals_total']} "
          f"Signale gefiltert ({skip_rate:.0f}%)")
    for side, r_list in [("LONG", result["long"]), ("SHORT", result["short"])]:
        if not r_list:
            print(f"  {asset} {side}  │  n=0")
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
    parser.add_argument("--assets",     default=",".join(ASSETS))
    parser.add_argument("--dir",        default="both", choices=["long", "short", "both"])
    parser.add_argument("--percentile", type=int, default=ATR_REGIME_PCT,
                        help="ATR-Perzentil-Schwelle (default 60)")
    parser.add_argument("--from",       dest="from_date", default=None)
    args = parser.parse_args()

    assets  = [a.strip().upper() for a in args.assets.split(",")]
    from_ts = None
    if args.from_date:
        from_ts = int(datetime.strptime(args.from_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp() * 1000)

    print(f"MTR-R Scout — 4H × {LOOKBACK} Bars (20d) + ATR-Regime > {args.percentile}. Pct (90d)")
    print(f"Chandelier: {ATR_MULT}×ATR({ATR_PERIOD})  |  Richtung: {args.dir.upper()}")

    all_longs:  list[float] = []
    all_shorts: list[float] = []
    total_sigs = 0
    total_skip = 0

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

        result = run_mtr_r(candles_4h, direction=args.dir,
                           regime_pct=args.percentile)
        print(f"\n{'═'*30} {asset} {'═'*30}")
        print_report(asset, result)
        all_longs.extend(result["long"])
        all_shorts.extend(result["short"])
        total_sigs += result["signals_total"]
        total_skip += result["skips_regime"]

    if len(assets) > 1:
        skip_rate = total_skip / total_sigs * 100 if total_sigs else 0
        print(f"\n{'═'*60}")
        print(f"  PORTFOLIO GESAMT  │  Regime-Filter: {total_skip}/{total_sigs} "
              f"Signale gefiltert ({skip_rate:.0f}%)")
        for side, r_list in [("LONG", all_longs), ("SHORT", all_shorts)]:
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
