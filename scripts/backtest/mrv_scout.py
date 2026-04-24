#!/usr/bin/env python3
"""
MRV Scout — Bollinger Band + RSI Mean Reversion
================================================
Paradigma: Kein Level-Crossing, keine Trend-Signale.
Stattdessen: statistisch überextendierter Preis → Rückkehr zur Mitte.

Signal (SHORT):
  - Close > upper_BB (Preis außerhalb 2σ nach oben)
  - RSI(14) > 70 (Momentum bestätigt Überextension)
  → SHORT Entry: market-close der Signal-Kerze
  → TP: middle_BB (SMA20) — der statistische Mittelwert
  → SL: 1.5×ATR(14) über dem Entry-Close

Signal (LONG):
  - Close < lower_BB (Preis außerhalb 2σ nach unten)
  - RSI(14) < 30
  → LONG Entry: market-close
  → TP: middle_BB (SMA20)
  → SL: 1.5×ATR(14) unter dem Entry-Close

Warum anderes als bisherige Fades:
  - Turtle Soup / LHU: fade nach EINEM Kerzen-Wende-Pattern (1-bar event)
  - MRV: statistische Überextension (RSI + 2σ BB) = seltener, stärker
  - TP ist dynamisch (Rückkehr zur Mitte), nicht fix in R

Usage:
  venv/bin/python3 scripts/backtest/mrv_scout.py
"""
import math
import os
import sys

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, aggregate_4h

TAKER_FEE = 0.0006
SLIPPAGE  = 0.0005
COST_R    = (TAKER_FEE + SLIPPAGE) * 2

# ─── Parameter ────────────────────────────────────────────────────────────────
BB_PERIOD  = 20
BB_MULT    = 2.0
RSI_PERIOD = 14
RSI_OB     = 70   # Overbought
RSI_OS     = 30   # Oversold
ATR_PERIOD = 14
SL_ATR     = 1.5  # SL-Abstand = 1.5×ATR (definiert 1R)

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = max(BB_PERIOD, RSI_PERIOD, ATR_PERIOD) + 10


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _sma_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1: i + 1]) / period
    return out


def _bb_series(candles: list[dict], period: int = BB_PERIOD, mult: float = BB_MULT):
    closes = [c["close"] for c in candles]
    upper  = [float("nan")] * len(candles)
    lower  = [float("nan")] * len(candles)
    mid    = [float("nan")] * len(candles)
    for i in range(period - 1, len(candles)):
        window = closes[i - period + 1: i + 1]
        sma    = sum(window) / period
        std    = math.sqrt(sum((x - sma) ** 2 for x in window) / period)
        mid[i]   = sma
        upper[i] = sma + mult * std
        lower[i] = sma - mult * std
    return upper, mid, lower


def _rsi_series(candles: list[dict], period: int = RSI_PERIOD) -> list[float]:
    out    = [float("nan")] * len(candles)
    closes = [c["close"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
        if i >= period:
            if i == period:
                avg_g = sum(gains[:period]) / period
                avg_l = sum(losses[:period]) / period
            else:
                avg_g = (avg_g * (period - 1) + gains[-1]) / period
                avg_l = (avg_l * (period - 1) + losses[-1]) / period
            rs       = avg_g / avg_l if avg_l > 0 else float("inf")
            out[i]   = 100 - 100 / (1 + rs)
    return out


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


# ─── KPIs ─────────────────────────────────────────────────────────────────────

def _p_approx(t_abs: float, df: int) -> float:
    if df <= 0: return 1.0
    z = t_abs * (1 - 1 / (4 * df)) / math.sqrt(1 + t_abs**2 / (2 * df))
    return max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))))


def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "pf": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "max_r": 0.0, "p": 1.0}
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r <= 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if n > 1:
        var = sum((r - avg_r) ** 2 for r in r_list) / (n - 1)
        std = math.sqrt(var) if var > 0 else 1e-9
        t   = avg_r / (std / math.sqrt(n))
    else:
        t = 0.0
    p = _p_approx(abs(t), n - 1) if n > 1 else 1.0
    return {
        "n": n, "avg_r": round(avg_r, 4), "wr": round(wr, 4),
        "pf": round(pf, 3), "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3), "max_r": round(max(r_list), 2), "p": round(p, 4),
    }


def ascii_dist(r_list: list[float], bins: int = 18) -> str:
    if not r_list: return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi: return f"    [{lo:.2f}R] alle gleich\n"
    width  = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r - lo) / width), bins - 1)] += 1
    max_c = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        label = lo + i * width
        bar   = "█" * int(c / max_c * 24)
        lines.append(f"    {label:+.2f}R │{bar} ({c})")
    return "\n".join(lines) + "\n"


# ─── Backtest ─────────────────────────────────────────────────────────────────

def run(candles: list[dict], direction: str) -> list[float]:
    """
    direction: 'long' (RSI<30 + close<lower_BB) oder 'short' (RSI>70 + close>upper_BB)
    """
    bb_up, bb_mid, bb_lo = _bb_series(candles)
    rsi  = _rsi_series(candles)
    atrs = _atr_series(candles)

    results  = []
    in_trade = False
    entry    = sl = tp_price = sl_pct = 0.0

    for i in range(WARMUP, len(candles)):
        if math.isnan(bb_up[i]) or math.isnan(rsi[i]) or math.isnan(atrs[i]):
            continue

        c = candles[i]

        if not in_trade:
            atr_val = atrs[i]

            if direction == "long":
                signal = c["close"] < bb_lo[i] and rsi[i] < RSI_OS
            else:
                signal = c["close"] > bb_up[i] and rsi[i] > RSI_OB

            if signal and not math.isnan(bb_mid[i]):
                entry    = c["close"]
                sl_dist  = atr_val * SL_ATR
                sl_pct   = sl_dist / entry
                tp_price = bb_mid[i]   # Rückkehr zur Mitte

                if direction == "long":
                    sl = entry - sl_dist
                    # Überprüfe ob TP sinnvoll (Mitte muss über Entry liegen)
                    if tp_price <= entry:
                        continue
                else:
                    sl = entry + sl_dist
                    if tp_price >= entry:
                        continue

                in_trade = True
                continue

        else:
            # TP: dynamisch = aktueller middle_BB (Mitte wandert mit)
            # Wir nehmen den BB-Mittel zum Zeitpunkt des Entry (statisch)
            # weil wir keinen Look-Ahead-Bias wollen
            if direction == "long":
                if c["low"] <= sl:
                    results.append(-1.0 - COST_R / sl_pct)
                    in_trade = False
                elif c["high"] >= tp_price:
                    tp_r = (tp_price - entry) / entry
                    results.append(tp_r / sl_pct - COST_R / sl_pct)
                    in_trade = False
            else:
                if c["high"] >= sl:
                    results.append(-1.0 - COST_R / sl_pct)
                    in_trade = False
                elif c["low"] <= tp_price:
                    tp_r = (entry - tp_price) / entry
                    results.append(tp_r / sl_pct - COST_R / sl_pct)
                    in_trade = False

    return results


# ─── Output ───────────────────────────────────────────────────────────────────

def _print_row(label: str, k: dict) -> None:
    print(
        f"  {label:<22} │  n={k['n']:>4}  AvgR={k['avg_r']:+.4f}R"
        f"  WR={k['wr']*100:.1f}%  PF={k['pf']:.2f}"
        f"  AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R"
        f"  p={k['p']:.4f}"
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    all_long, all_short = [], []

    print("═" * 72)
    print("  MRV SCOUT — Bollinger Band (20, 2σ) + RSI(14) Mean Reversion")
    print("  LONG:  Close < lower_BB AND RSI < 30  →  TP = SMA(20)")
    print("  SHORT: Close > upper_BB AND RSI > 70  →  TP = SMA(20)")
    print(f"  SL: {SL_ATR}×ATR(14)  │  Timeframe: 4H  │  Assets: {len(ASSETS)}")
    print("═" * 72)

    for asset in ASSETS:
        longs  = run(aggregate_4h(load_csv(asset, "15m")), "long")
        shorts = run(aggregate_4h(load_csv(asset, "15m")), "short")

        all_long  += longs
        all_short += shorts

        print(f"\n  {asset}")
        _print_row("MRV LONG",  kpis(longs))
        _print_row("MRV SHORT", kpis(shorts))

    # Portfolio
    combined = all_long + all_short
    kl = kpis(all_long)
    ks = kpis(all_short)
    kc = kpis(combined)

    print("\n" + "═" * 72)
    print("  PORTFOLIO GESAMT")
    print("─" * 72)
    _print_row("MRV LONG",     kl)
    _print_row("MRV SHORT",    ks)
    _print_row("MRV KOMBINIERT", kc)

    print("\n  R-Verteilung (KOMBINIERT):")
    print(ascii_dist(combined))

    print("═" * 72)
    print("  ENTSCHEIDUNG")
    if kc["avg_r"] > 0 and kc["p"] < 0.05 and kc["n"] >= 50:
        print(f"  ✅ GO — AvgR={kc['avg_r']:+.4f}R  p={kc['p']:.4f}  n={kc['n']}")
    elif kc["avg_r"] > 0:
        print(f"  ⚠️  Positiv aber nicht signifikant — AvgR={kc['avg_r']:+.4f}R  p={kc['p']:.4f}  n={kc['n']}")
    else:
        print(f"  ❌ NO-GO — AvgR={kc['avg_r']:+.4f}R  p={kc['p']:.4f}  n={kc['n']}")
    print("═" * 72)


if __name__ == "__main__":
    main()
