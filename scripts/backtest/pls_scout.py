#!/usr/bin/env python3
"""
PLS Scout — Phantom Liquidity Sweep
=====================================
Hypothese: Kerzen die ein 15-Perioden-Lokalhoch/-Tief mit langer Rejection-Wick
und erhöhtem Volumen sweepen, markieren institutionelle Stop-Runs.

SHORT-Setup (Liquidity-Sweep Hoch):
  - High > Highest(High, 15) der vorherigen 15 Kerzen
  - Close < Open (bearisch)
  - Upper Wick > WICK_MULT * Body
  - Lower Wick < LOWER_WICK_MULT * Body
  - Volume > VOL_MULT * Vol_SMA(VOL_SMA_PERIOD)
  - Entry: Sell-Stop am Low, gültig ENTRY_WINDOW Kerzen
  - SL:    High der Sweep-Kerze
  - TP:    3R

LONG-Setup (Liquidity-Sweep Tief) — symmetrisch:
  - Low < Lowest(Low, 15) der vorherigen 15 Kerzen
  - Close > Open (bullisch)
  - Lower Wick > WICK_MULT * Body
  - Upper Wick < LOWER_WICK_MULT * Body
  - Volume > VOL_MULT * Vol_SMA(VOL_SMA_PERIOD)
  - Entry: Buy-Stop am High, gültig ENTRY_WINDOW Kerzen
  - SL:    Low der Sweep-Kerze
  - TP:    3R

Daten: 15m-CSVs aggregiert zu 1H.
Usage:
  venv/bin/python3 scripts/backtest/pls_scout.py
  venv/bin/python3 scripts/backtest/pls_scout.py --assets ETH,SOL --dir short
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

TAKER_FEE  = 0.0006
SLIPPAGE   = 0.0005

# ─── Parameter ────────────────────────────────────────────────────────────────
LOOKBACK         = 15     # Kerzen für lokales Hoch/Tief (ohne aktuelle)
WICK_MULT        = 2.0    # Upper/Lower Wick muss > WICK_MULT × Body
LOWER_WICK_MULT  = 1.0    # Gegenseitiger Docht < LOWER_WICK_MULT × Body
VOL_MULT         = 1.2    # Volumen > VOL_MULT × SMA(VOL_SMA_PERIOD)
VOL_SMA_PERIOD   = 50     # Vol-SMA-Periode
TP_R             = 3.0
ENTRY_WINDOW     = 2      # Kerzen in denen der Sell/Buy-Stop aktiv ist

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = VOL_SMA_PERIOD + LOOKBACK + 5

# ─── 1H-Aggregation ───────────────────────────────────────────────────────────

def aggregate_1h(candles_15m: list[dict]) -> list[dict]:
    if not candles_15m:
        return []
    buckets: dict[int, dict] = {}
    for c in candles_15m:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        from datetime import datetime as _dt
        bucket_ts = int(_dt(dt.year, dt.month, dt.day, dt.hour,
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


# ─── Hilfs-Serien ─────────────────────────────────────────────────────────────

def _vol_sma_series(candles: list[dict], period: int = VOL_SMA_PERIOD) -> list[float]:
    out = [float("nan")] * len(candles)
    buf = []
    for i, c in enumerate(candles):
        buf.append(c["volume"])
        if len(buf) > period:
            buf.pop(0)
        if len(buf) == period:
            out[i] = sum(buf) / period
    return out


def _rolling_highest(candles: list[dict], lookback: int) -> list[float]:
    """Highest High der letzten `lookback` Kerzen EXKLUSIV der aktuellen."""
    out = [float("nan")] * len(candles)
    for i in range(lookback, len(candles)):
        out[i] = max(c["high"] for c in candles[i - lookback:i])
    return out


def _rolling_lowest(candles: list[dict], lookback: int) -> list[float]:
    """Lowest Low der letzten `lookback` Kerzen EXKLUSIV der aktuellen."""
    out = [float("nan")] * len(candles)
    for i in range(lookback, len(candles)):
        out[i] = min(c["low"] for c in candles[i - lookback:i])
    return out


# ─── KPIs ─────────────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0, "wr": 0, "pf": 0,
                "total_r": 0, "max_dd": 0, "sharpe": 0, "t": 0, "p": 1.0}
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r < 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    # Max Drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_list:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    # Sharpe (annualisiert, 24×365 = 8760 1H-Kerzen/Jahr)
    if n > 1:
        variance = sum((r - avg_r) ** 2 for r in r_list) / (n - 1)
        std = math.sqrt(variance) if variance > 0 else 1e-9
        sharpe = (avg_r / std) * math.sqrt(8760)
    else:
        std, sharpe = 0, 0
    # t-Test
    t = (avg_r / (std / math.sqrt(n))) if std > 0 and n > 1 else 0
    # p-Wert (Näherung via t-Verteilung, zweiseitig)
    def _p_from_t(t_val, df):
        x = df / (df + t_val ** 2)
        # Regularized incomplete beta — Näherung via iterativer Summe
        if df <= 0:
            return 1.0
        a, b = df / 2, 0.5
        # Verwende Normal-Approximation für große df
        if df > 30:
            z = abs(t_val)
            p = 2 * (1 - _norm_cdf(z))
        else:
            p = _t_cdf_approx(abs(t_val), df)
        return p

    def _norm_cdf(z):
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def _t_cdf_approx(t_val, df):
        # Abramowitz & Stegun Näherung
        x = df / (df + t_val ** 2)
        p_half = 1 - 0.5 * _ibeta(x, df / 2, 0.5)
        return 2 * (1 - p_half)

    def _ibeta(x, a, b):
        # Einfache Näherung via continued fraction für Beta-Funktion
        if x <= 0: return 0
        if x >= 1: return 1
        lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
        front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
        # Lentz continued fraction
        TINY = 1e-30
        f = TINY
        C, D = f, 0.0
        for m in range(200):
            for idx in [0, 1]:
                if idx == 0:
                    d = -(a + m) * (a + b + m) * x / ((a + 2*m) * (a + 2*m + 1))
                else:
                    d = (m + 1) * (b - m - 1) * x / ((a + 2*m + 1) * (a + 2*m + 2))
                D = 1 + d * D
                if abs(D) < TINY: D = TINY
                C = 1 + d / C
                if abs(C) < TINY: C = TINY
                D = 1 / D
                delta = C * D
                f *= delta
                if abs(delta - 1) < 1e-8:
                    break
        return front * f

    p_val = _p_from_t(abs(t), n - 1) if n > 1 else 1.0
    p_val = max(0.0, min(1.0, p_val))

    return {
        "n": n, "avg_r": round(avg_r, 4), "wr": round(wr, 4),
        "pf": round(pf, 3), "total_r": round(sum(r_list), 2),
        "max_dd": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "t": round(t, 3), "p": round(p_val, 4),
    }


def ascii_dist(r_list: list[float], bins: int = 20) -> str:
    if not r_list:
        return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi:
        return f"[{lo:.2f}] alle gleich"
    width = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        idx = min(int((r - lo) / width), bins - 1)
        counts[idx] += 1
    max_c = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        bar_lo = lo + i * width
        bar = "█" * int(c / max_c * 30)
        lines.append(f"  {bar_lo:+6.2f}R │{bar} ({c})")
    return "\n".join(lines)


# ─── Backtest-Core ────────────────────────────────────────────────────────────

def run_pls(candles: list[dict], direction: str = "both") -> dict:
    """
    direction: "short" | "long" | "both"
    Gibt {"short": [r,...], "long": [r,...], "signals": int, "skips": int} zurück.
    """
    vol_sma  = _vol_sma_series(candles)
    high_max = _rolling_highest(candles, LOOKBACK)
    low_min  = _rolling_lowest(candles, LOOKBACK)

    shorts: list[float] = []
    longs:  list[float] = []
    skips   = 0
    signals = 0

    # Pending-Entry-State
    pending: dict | None = None   # {side, entry, sl, tp, expires_at}

    for i in range(WARMUP, len(candles)):
        c = candles[i]

        # ── 1. Offenes Pending abarbeiten ─────────────────────────────────────
        if pending is not None:
            if i > pending["expires_at"]:
                pending = None
            else:
                if pending["side"] == "short":
                    # Sell-Stop: Entry wenn Low <= Pendingpreis
                    if c["low"] <= pending["entry"]:
                        entry = pending["entry"]
                        sl    = pending["sl"]
                        risk  = sl - entry
                        tp    = entry - risk * TP_R
                        fees_r = (TAKER_FEE + SLIPPAGE) * 2 * entry / risk
                        # SL-first innerhalb dieser Kerze
                        r_gross = None
                        if c["high"] >= sl:     # SL getroffen
                            r_gross = -1.0
                        elif c["low"] <= tp:    # TP getroffen
                            r_gross = TP_R
                        else:
                            # Offen — wird in Folgekerzen aufgelöst
                            # Vereinfachung: weiter schauen (manage_trade)
                            r_gross = _manage_trade(candles, i + 1, entry, sl, tp,
                                                    is_short=True)
                        if r_gross is not None:
                            r_net = r_gross - fees_r if r_gross > 0 else r_gross - fees_r
                            shorts.append(round(r_net, 3))
                        pending = None
                        continue

                else:  # long
                    # Buy-Stop: Entry wenn High >= Pendingpreis
                    if c["high"] >= pending["entry"]:
                        entry = pending["entry"]
                        sl    = pending["sl"]
                        risk  = entry - sl
                        tp    = entry + risk * TP_R
                        fees_r = (TAKER_FEE + SLIPPAGE) * 2 * entry / risk
                        r_gross = None
                        if c["low"] <= sl:
                            r_gross = -1.0
                        elif c["high"] >= tp:
                            r_gross = TP_R
                        else:
                            r_gross = _manage_trade(candles, i + 1, entry, sl, tp,
                                                    is_short=False)
                        if r_gross is not None:
                            r_net = r_gross - fees_r if r_gross > 0 else r_gross - fees_r
                            longs.append(round(r_net, 3))
                        pending = None
                        continue

        # ── 2. Signal-Erkennung ───────────────────────────────────────────────
        vol_s = vol_sma[i]
        h_max = high_max[i]
        l_min = low_min[i]

        if math.isnan(vol_s) or math.isnan(h_max):
            skips += 1
            continue

        body        = abs(c["close"] - c["open"])
        upper_wick  = c["high"] - max(c["open"], c["close"])
        lower_wick  = min(c["open"], c["close"]) - c["low"]

        if body < 1e-9:  # Doji mit body=0 vermeiden
            skips += 1
            continue

        # SHORT-Setup
        if direction in ("short", "both"):
            if (c["high"] > h_max                          # Sweep
                    and c["close"] < c["open"]             # bearisch
                    and upper_wick > WICK_MULT * body      # langer Wick oben
                    and lower_wick < LOWER_WICK_MULT * body  # kurzer Wick unten
                    and c["volume"] > VOL_MULT * vol_s):   # Volumen-Effort
                sl = c["high"]
                entry = c["low"]
                risk = sl - entry
                if risk < 1e-6:
                    skips += 1
                    continue
                signals += 1
                pending = {
                    "side": "short",
                    "entry": entry,
                    "sl": sl,
                    "expires_at": i + ENTRY_WINDOW,
                }

        # LONG-Setup (symmetrisch, kein Signal wenn bereits Short-Pending)
        if direction in ("long", "both") and pending is None:
            if (c["low"] < l_min                           # Sweep
                    and c["close"] > c["open"]             # bullisch
                    and lower_wick > WICK_MULT * body      # langer Wick unten
                    and upper_wick < LOWER_WICK_MULT * body  # kurzer Wick oben
                    and c["volume"] > VOL_MULT * vol_s):   # Volumen-Effort
                sl = c["low"]
                entry = c["high"]
                risk = entry - sl
                if risk < 1e-6:
                    skips += 1
                    continue
                signals += 1
                pending = {
                    "side": "long",
                    "entry": entry,
                    "sl": sl,
                    "expires_at": i + ENTRY_WINDOW,
                }

    return {"short": shorts, "long": longs,
            "signals": signals, "skips": skips}


def _manage_trade(candles: list[dict], start: int,
                  entry: float, sl: float, tp: float,
                  is_short: bool) -> float:
    """Verfolgt eine offene Position bar-by-bar bis SL oder TP getroffen."""
    for j in range(start, min(start + 200, len(candles))):
        c = candles[j]
        if is_short:
            if c["high"] >= sl: return -1.0
            if c["low"]  <= tp: return TP_R
        else:
            if c["low"]  <= sl: return -1.0
            if c["high"] >= tp: return TP_R
    return -1.0  # Timeout → SL (konservativ)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(assets: list[str] | None = None, direction: str = "both"):
    assets = assets or ASSETS

    all_short: list[float] = []
    all_long:  list[float] = []
    total_signals = 0

    print(f"\n{'='*62}")
    print(f"  PLS Scout — Phantom Liquidity Sweep")
    print(f"  Lookback={LOOKBACK}  Wick>{WICK_MULT}×Body  Vol>{VOL_MULT}×SMA({VOL_SMA_PERIOD})")
    print(f"  Entry-Window={ENTRY_WINDOW}  TP={TP_R}R  Direction={direction}")
    print(f"{'='*62}")

    for asset in assets:
        raw = load_csv(asset, "15m")
        if not raw:
            print(f"  {asset}: keine Daten")
            continue
        candles = aggregate_1h(raw)
        if len(candles) < WARMUP + 20:
            print(f"  {asset}: zu wenig Kerzen ({len(candles)})")
            continue

        res = run_pls(candles, direction)
        s, l = res["short"], res["long"]
        total_signals += res["signals"]

        ks = kpis(s) if s else None
        kl = kpis(l) if l else None

        print(f"\n  ── {asset} ({len(candles)} 1H-Kerzen) ──")
        if s:
            print(f"    SHORT  n={ks['n']:3d}  AvgR={ks['avg_r']:+.3f}  "
                  f"WR={ks['wr']*100:.0f}%  PF={ks['pf']:.2f}  "
                  f"TotalR={ks['total_r']:+.1f}  p={ks['p']:.4f}")
        else:
            print(f"    SHORT  n=  0  — kein Signal")

        if l:
            print(f"    LONG   n={kl['n']:3d}  AvgR={kl['avg_r']:+.3f}  "
                  f"WR={kl['wr']*100:.0f}%  PF={kl['pf']:.2f}  "
                  f"TotalR={kl['total_r']:+.1f}  p={kl['p']:.4f}")
        else:
            print(f"    LONG   n=  0  — kein Signal")

        all_short.extend(s)
        all_long.extend(l)

    # Kombinierter Report
    print(f"\n{'='*62}")
    print(f"  GESAMT — {len(assets)} Assets, {total_signals} Signal-Setups erkannt")
    print(f"{'='*62}")

    for label, rs in [("SHORT", all_short), ("LONG", all_long), ("BEIDE", all_short + all_long)]:
        if not rs:
            print(f"\n  {label}: keine Trades")
            continue
        k = kpis(rs)
        print(f"\n  {label}:")
        print(f"    n={k['n']}  AvgR={k['avg_r']:+.4f}  WR={k['wr']*100:.1f}%")
        print(f"    PF={k['pf']:.3f}  TotalR={k['total_r']:+.1f}R")
        print(f"    Sharpe={k['sharpe']:.2f}  MaxDD={k['max_dd']:.2f}R")
        print(f"    t={k['t']:.3f}  p={k['p']:.4f}  "
              + ("✅ p<0.05" if k['p'] < 0.05 else "❌ p≥0.05"))
        print(f"\n    R-Verteilung:")
        print(ascii_dist(rs))

    print(f"\n{'='*62}")
    print(f"  Scout-Gate: Avg R > 0 | p < 0.05 | n > 30")
    for label, rs in [("SHORT", all_short), ("LONG", all_long)]:
        if not rs:
            print(f"  {label}: KEIN SIGNAL")
            continue
        k = kpis(rs)
        go = k['avg_r'] > 0 and k['p'] < 0.05 and k['n'] >= 30
        status = "✅ GO  → WFA starten" if go else "❌ NO-GO"
        print(f"  {label}: AvgR={k['avg_r']:+.4f}  p={k['p']:.4f}  "
              f"n={k['n']}  → {status}")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(ASSETS))
    parser.add_argument("--dir", choices=["short", "long", "both"], default="both")
    args = parser.parse_args()
    main(args.assets.split(","), direction=args.dir)
