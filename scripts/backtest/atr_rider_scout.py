#!/usr/bin/env python3
"""
ATR-Rider Scout — Adaptive Trend Rider
=======================================
Hypothese: 48H-Donchian-Breakouts mit Volumen-Confirmation leiten in Krypto
seltene, aber gigantische Trendbewegungen ein. Ein Chandelier-Trailing-Stop
(nie sinkend) lässt Gewinner laufen und schneidet Verlierer bei ~-1R ab.

Signal-Logik:
  LONG:  Close > Highest(High, 48 Bars)  +  Vol > 1.2×SMA(50)
  SHORT: Close < Lowest(Low, 48 Bars)    +  Vol > 1.2×SMA(50)

Entry:  Market-Close der Ausbruchskerze
Exit:   Chandelier Trailing Stop
  - Start:  Entry ± 2.5×ATR(14)
  - Update: Max/Min(prev_SL, Peak ∓ 2.5×ATR(14))  — darf nie gegen uns ziehen
  - Hit:    Low ≤ SL (LONG) / High ≥ SL (SHORT)

R-Metrik: (Exit − Entry) / Initial_Risk   wobei Initial_Risk = 2.5×ATR@Entry
  - Verlust: ≈ −1R (sofort gestoppt) bis 0R (SL nachgezogen, aber knapp)
  - Gewinn:  unbegrenzt — Strategie lebt von der rechten Seite der Verteilung

Kein TP. Kein Timeout. Die R-Verteilung ist rechts-schief (positiv skewed),
nicht bimodal wie bei Fixed-TP-Strategien.

Usage:
  venv/bin/python3 scripts/backtest/atr_rider_scout.py
  venv/bin/python3 scripts/backtest/atr_rider_scout.py --assets ETH,SOL --dir long
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv

TAKER_FEE = 0.0006
SLIPPAGE  = 0.0005

# ─── Parameter ────────────────────────────────────────────────────────────────
LOOKBACK     = 48    # 48H-Donchian (Highest High / Lowest Low der letzten 48 Bars)
ATR_PERIOD   = 14
ATR_MULT     = 2.5   # Chandelier-Faktor
VOL_PERIOD   = 50
VOL_MULT     = 1.2

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = LOOKBACK + max(ATR_PERIOD, VOL_PERIOD) + 5


# ─── 1H-Aggregation ───────────────────────────────────────────────────────────

def aggregate_1h(candles_15m: list[dict]) -> list[dict]:
    if not candles_15m:
        return []
    buckets: dict[int, dict] = {}
    for c in candles_15m:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        bucket_ts = int(datetime(dt.year, dt.month, dt.day, dt.hour,
                                 tzinfo=timezone.utc).timestamp() * 1000)
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {
                "time": bucket_ts, "open": c["open"],
                "high": c["high"], "low": c["low"],
                "close": c["close"], "volume": c["volume"],
            }
        else:
            b = buckets[bucket_ts]
            b["high"]   = max(b["high"], c["high"])
            b["low"]    = min(b["low"],  c["low"])
            b["close"]  = c["close"]
            b["volume"] += c["volume"]
    return sorted(buckets.values(), key=lambda x: x["time"])


# ─── Indikator-Serien ─────────────────────────────────────────────────────────

def _atr_series(candles: list[dict], period: int = ATR_PERIOD) -> list[float]:
    """Wilder's ATR — O(n)."""
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


# ─── KPIs (rechts-schiefe Verteilung: Expectancy statt klassisches R) ─────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0, "wr": 0, "pf": 0, "expectancy": 0,
                "total_r": 0, "max_dd": 0, "sharpe": 0, "t": 0, "p": 1.0,
                "p10": 0, "p50": 0, "p90": 0, "max_r": 0}
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r <= 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    # Equity-Kurve + Max DD
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_list:
        equity += r
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    # Sharpe
    if n > 1:
        var    = sum((r - avg_r) ** 2 for r in r_list) / (n - 1)
        std    = math.sqrt(var) if var > 0 else 1e-9
        sharpe = (avg_r / std) * math.sqrt(8760)
        t      = avg_r / (std / math.sqrt(n))
    else:
        std, sharpe, t = 0, 0, 0
    p = _p_approx(abs(t), n - 1) if n > 1 else 1.0
    # Perzentile (wichtig für rechts-schiefe Verteilung)
    s = sorted(r_list)
    p10 = s[max(0, int(n * 0.10))]
    p50 = s[int(n * 0.50)]
    p90 = s[min(n-1, int(n * 0.90))]
    # Expectancy = WR × AvgWin − (1−WR) × AvgLoss  [in R]
    avg_win  = sum(wins)  / len(wins)  if wins   else 0
    avg_loss = sum(losses)/ len(losses) if losses else 0
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


def _p_approx(t_abs: float, df: int) -> float:
    if df <= 0: return 1.0
    z = t_abs * (1 - 1 / (4 * df)) / math.sqrt(1 + t_abs**2 / (2 * df))
    return max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))))


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

def run_atr_rider(candles: list[dict], direction: str = "both") -> dict:
    atr_s    = _atr_series(candles, ATR_PERIOD)
    vol_sma  = _sma_series([c["volume"] for c in candles], VOL_PERIOD)
    high_max = _rolling_highest(candles, LOOKBACK)   # exklusiv aktuelle Bar
    low_min  = _rolling_lowest(candles, LOOKBACK)    # exklusiv aktuelle Bar

    longs:  list[float] = []
    shorts: list[float] = []
    trade:  dict | None = None

    for i in range(WARMUP, len(candles)):
        c    = candles[i]
        atr  = atr_s[i]
        vsma = vol_sma[i]
        hmax = high_max[i]   # Highest der letzten 48 Bars VOR dieser Kerze
        lmin = low_min[i]    # Lowest der letzten 48 Bars VOR dieser Kerze

        if math.isnan(atr) or math.isnan(vsma) or math.isnan(hmax):
            continue

        # ── 1. Offenen Trade managen ──────────────────────────────────────────
        if trade is not None:
            current_atr = atr

            if trade["side"] == "long":
                # Peak nachziehen
                if c["high"] > trade["peak"]:
                    trade["peak"] = c["high"]
                # SL nachziehen (darf nie sinken)
                new_sl = trade["peak"] - ATR_MULT * current_atr
                trade["sl"] = max(trade["sl"], new_sl)
                # SL-Check (SL-first)
                if c["low"] <= trade["sl"]:
                    exit_p    = trade["sl"]
                    r_gross   = (exit_p - trade["entry"]) / trade["initial_risk"]
                    fees_r    = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    longs.append(round(r_gross - fees_r, 3))
                    trade = None

            else:  # short
                if c["low"] < trade["peak"]:
                    trade["peak"] = c["low"]
                new_sl = trade["peak"] + ATR_MULT * current_atr
                trade["sl"] = min(trade["sl"], new_sl)
                if c["high"] >= trade["sl"]:
                    exit_p    = trade["sl"]
                    r_gross   = (trade["entry"] - exit_p) / trade["initial_risk"]
                    fees_r    = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    shorts.append(round(r_gross - fees_r, 3))
                    trade = None

            if trade is not None:
                continue   # Trade läuft noch

        # ── 2. Signal-Erkennung (nur wenn kein Trade offen) ──────────────────
        vol_ok = c["volume"] > VOL_MULT * vsma

        if direction in ("long", "both") and vol_ok and c["close"] > hmax:
            entry         = c["close"]
            initial_risk  = ATR_MULT * atr
            if initial_risk < 1e-6:
                continue
            trade = {
                "side":         "long",
                "entry":        entry,
                "initial_risk": initial_risk,
                "sl":           entry - initial_risk,
                "peak":         entry,
            }

        elif direction in ("short", "both") and vol_ok and c["close"] < lmin:
            entry         = c["close"]
            initial_risk  = ATR_MULT * atr
            if initial_risk < 1e-6:
                continue
            trade = {
                "side":         "short",
                "entry":        entry,
                "initial_risk": initial_risk,
                "sl":           entry + initial_risk,
                "peak":         entry,
            }

    return {"long": longs, "short": shorts}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(assets: list[str] | None = None, direction: str = "both"):
    assets = assets or ASSETS

    all_long:  list[float] = []
    all_short: list[float] = []

    print(f"\n{'='*66}")
    print(f"  ATR-Rider Scout — Adaptive Trend Rider (Chandelier Exit)")
    print(f"  Lookback={LOOKBACK}H  ATR({ATR_PERIOD})×{ATR_MULT}  Vol>{VOL_MULT}×SMA({VOL_PERIOD})")
    print(f"  Kein TP — Trail-SL nie sinkend  |  Direction={direction}")
    print(f"{'='*66}")

    for asset in assets:
        raw = load_csv(asset, "15m")
        if not raw:
            print(f"  {asset}: keine Daten")
            continue
        candles = aggregate_1h(raw)
        if len(candles) < WARMUP + 20:
            print(f"  {asset}: zu wenig Kerzen")
            continue

        res = run_atr_rider(candles, direction)
        l, s = res["long"], res["short"]
        kl   = kpis(l) if l else None
        ks   = kpis(s) if s else None

        print(f"\n  ── {asset} ({len(candles)} 1H-Kerzen) ──")
        if l:
            print(f"    LONG   n={kl['n']:3d}  AvgR={kl['avg_r']:+.3f}  WR={kl['wr']*100:.0f}%  "
                  f"PF={kl['pf']:.2f}  MaxR={kl['max_r']:+.1f}  p={kl['p']:.4f}")
        else:
            print(f"    LONG   n=  0")
        if s:
            print(f"    SHORT  n={ks['n']:3d}  AvgR={ks['avg_r']:+.3f}  WR={ks['wr']*100:.0f}%  "
                  f"PF={ks['pf']:.2f}  MaxR={ks['max_r']:+.1f}  p={ks['p']:.4f}")
        else:
            print(f"    SHORT  n=  0")

        all_long.extend(l)
        all_short.extend(s)

    # Gesamt-Report
    print(f"\n{'='*66}")
    print(f"  GESAMT — {len(assets)} Assets")
    print(f"{'='*66}")

    for label, rs in [("LONG", all_long), ("SHORT", all_short),
                      ("BEIDE", all_long + all_short)]:
        if not rs:
            print(f"\n  {label}: keine Trades")
            continue
        k = kpis(rs)
        print(f"\n  {label}  (n={k['n']}):")
        print(f"    AvgR={k['avg_r']:+.4f}  WR={k['wr']*100:.1f}%  PF={k['pf']:.3f}")
        print(f"    AvgWin={k['avg_win']:+.2f}R  AvgLoss={k['avg_loss']:+.2f}R")
        print(f"    Expectancy={k['expectancy']:+.4f}R  TotalR={k['total_r']:+.1f}R")
        print(f"    Sharpe={k['sharpe']:.2f}  MaxDD={k['max_dd']:.2f}R")
        print(f"    Perzentile: P10={k['p10']:+.2f}R  P50={k['p50']:+.2f}R  "
              f"P90={k['p90']:+.2f}R  Max={k['max_r']:+.2f}R")
        print(f"    t={k['t']:.3f}  p={k['p']:.4f}  "
              + ("✅ p<0.05" if k['p'] < 0.05 else "❌ p≥0.05"))
        print(f"\n    R-Verteilung (rechts-schief erwartet):")
        print(ascii_dist(rs))

    # Scout-Gate
    print(f"\n{'='*66}")
    print(f"  Scout-Gate: AvgR > 0 | p < 0.05 | n ≥ 30")
    for label, rs in [("LONG", all_long), ("SHORT", all_short)]:
        if not rs:
            print(f"  {label}: KEIN SIGNAL")
            continue
        k = kpis(rs)
        go = k['avg_r'] > 0 and k['p'] < 0.05 and k['n'] >= 30
        print(f"  {label}: AvgR={k['avg_r']:+.4f}  p={k['p']:.4f}  "
              f"n={k['n']}  → {'✅ GO → WFA' if go else '❌ NO-GO'}")
    print(f"{'='*66}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(ASSETS))
    parser.add_argument("--dir",    choices=["long", "short", "both"], default="both")
    args = parser.parse_args()
    main(args.assets.split(","), direction=args.dir)
