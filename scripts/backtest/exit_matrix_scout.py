#!/usr/bin/env python3
"""
Exit Matrix Scout — TFR Entry + Fixed Exits
============================================
Entry:  EMA(20) kreuzt EMA(55) auf 4H-Chart (identisch zu state_scout.py)
SL:     1.5 × ATR(14) unter/über Entry-Close (fest, kein Trail)

Drei Exit-Varianten:
  V1 — Scalper:      TP = 1.0R, SL = 1.0R
  V2 — Asymmetrisch: TP = 1.5R, SL = 1.0R
  V3 — Time-Stop:    Exit nach 18 Kerzen (3 Tage), Notfall-SL = -3.0R

Hypothese: Krypto ist zu 'peitschig' für Trailing-Stops.
Ein fixer TP im 1–1.5R-Korridor hebt die WR auf >40% und macht den Edge profitabel.

Usage:
  venv/bin/python3 scripts/backtest/exit_matrix_scout.py
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
COST_R    = (TAKER_FEE + SLIPPAGE) * 2  # entry + exit round-trip

# ─── Parameter ────────────────────────────────────────────────────────────────
EMA_FAST   = 20
EMA_SLOW   = 55
ATR_PERIOD = 14
SL_ATR_MULT = 1.5   # SL-Abstand = 1.5 × ATR → definiert 1R

# Exit-Varianten
V1_TP = 1.0
V2_TP = 1.5
V3_BARS = 18        # 18 × 4H = 3 Tage
V3_EMERGENCY_SL = -3.0

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = EMA_SLOW + ATR_PERIOD + 10


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _ema_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i-1] * (1 - k)
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
        "n": n,
        "avg_r":    round(avg_r, 4),
        "wr":       round(wr, 4),
        "pf":       round(pf, 3),
        "avg_win":  round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "max_r":    round(max(r_list), 2),
        "p":        round(p, 4),
    }


def ascii_dist(r_list: list[float], bins: int = 16) -> str:
    if not r_list: return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi: return f"  [{lo:.2f}R] alle gleich\n"
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


# ─── R-Berechnung (Fees/Slippage) ────────────────────────────────────────────

def _to_r(pnl_pct: float, sl_pct: float) -> float:
    """Konvertiert PnL% in R-Multiplikator, abzüglich Kosten."""
    gross_r = pnl_pct / sl_pct
    return gross_r - COST_R / sl_pct


# ─── Backtest: V1 — Scalper (TP=1R, SL=1R) ──────────────────────────────────

def run_v1(candles: list[dict], direction: str) -> list[float]:
    closes  = [c["close"] for c in candles]
    e20     = _ema_series(closes, EMA_FAST)
    e55     = _ema_series(closes, EMA_SLOW)
    atrs    = _atr_series(candles)
    results = []
    in_trade = False

    for i in range(WARMUP, len(candles)):
        if math.isnan(e20[i]) or math.isnan(e55[i]) or math.isnan(atrs[i]):
            continue

        # Entry: Crossover-Signal
        if not in_trade:
            bull_x = e20[i-1] <= e55[i-1] and e20[i] > e55[i]
            bear_x = e20[i-1] >= e55[i-1] and e20[i] < e55[i]
            if (direction == "long" and bull_x) or (direction == "short" and bear_x):
                entry   = candles[i]["close"]
                atr_val = atrs[i]
                sl_dist = atr_val * SL_ATR_MULT      # 1R = SL-Abstand
                sl_pct  = sl_dist / entry
                tp_dist = sl_dist * V1_TP
                if direction == "long":
                    sl  = entry - sl_dist
                    tp  = entry + tp_dist
                else:
                    sl  = entry + sl_dist
                    tp  = entry - tp_dist
                in_trade = True
                continue

        else:
            c = candles[i]
            if direction == "long":
                if c["low"] <= sl:
                    results.append(_to_r(-sl_pct, sl_pct))
                    in_trade = False
                elif c["high"] >= tp:
                    results.append(_to_r(+sl_pct * V1_TP, sl_pct))
                    in_trade = False
            else:
                if c["high"] >= sl:
                    results.append(_to_r(-sl_pct, sl_pct))
                    in_trade = False
                elif c["low"] <= tp:
                    results.append(_to_r(+sl_pct * V1_TP, sl_pct))
                    in_trade = False

    return results


# ─── Backtest: V2 — Asymmetrisch (TP=1.5R, SL=1R) ───────────────────────────

def run_v2(candles: list[dict], direction: str) -> list[float]:
    closes  = [c["close"] for c in candles]
    e20     = _ema_series(closes, EMA_FAST)
    e55     = _ema_series(closes, EMA_SLOW)
    atrs    = _atr_series(candles)
    results = []
    in_trade = False

    for i in range(WARMUP, len(candles)):
        if math.isnan(e20[i]) or math.isnan(e55[i]) or math.isnan(atrs[i]):
            continue

        if not in_trade:
            bull_x = e20[i-1] <= e55[i-1] and e20[i] > e55[i]
            bear_x = e20[i-1] >= e55[i-1] and e20[i] < e55[i]
            if (direction == "long" and bull_x) or (direction == "short" and bear_x):
                entry   = candles[i]["close"]
                atr_val = atrs[i]
                sl_dist = atr_val * SL_ATR_MULT
                sl_pct  = sl_dist / entry
                tp_dist = sl_dist * V2_TP
                if direction == "long":
                    sl = entry - sl_dist
                    tp = entry + tp_dist
                else:
                    sl = entry + sl_dist
                    tp = entry - tp_dist
                in_trade = True
                continue

        else:
            c = candles[i]
            if direction == "long":
                if c["low"] <= sl:
                    results.append(_to_r(-sl_pct, sl_pct))
                    in_trade = False
                elif c["high"] >= tp:
                    results.append(_to_r(+sl_pct * V2_TP, sl_pct))
                    in_trade = False
            else:
                if c["high"] >= sl:
                    results.append(_to_r(-sl_pct, sl_pct))
                    in_trade = False
                elif c["low"] <= tp:
                    results.append(_to_r(+sl_pct * V2_TP, sl_pct))
                    in_trade = False

    return results


# ─── Backtest: V3 — Time-Stop (18 Bars = 3 Tage) ────────────────────────────

def run_v3(candles: list[dict], direction: str) -> list[float]:
    closes  = [c["close"] for c in candles]
    e20     = _ema_series(closes, EMA_FAST)
    e55     = _ema_series(closes, EMA_SLOW)
    atrs    = _atr_series(candles)
    results = []
    in_trade = False
    entry_bar = 0

    for i in range(WARMUP, len(candles)):
        if math.isnan(e20[i]) or math.isnan(e55[i]) or math.isnan(atrs[i]):
            continue

        if not in_trade:
            bull_x = e20[i-1] <= e55[i-1] and e20[i] > e55[i]
            bear_x = e20[i-1] >= e55[i-1] and e20[i] < e55[i]
            if (direction == "long" and bull_x) or (direction == "short" and bear_x):
                entry     = candles[i]["close"]
                atr_val   = atrs[i]
                sl_dist   = atr_val * SL_ATR_MULT
                sl_pct    = sl_dist / entry
                emerg_sl  = entry - sl_dist * 3.0 if direction == "long" else entry + sl_dist * 3.0
                in_trade  = True
                entry_bar = i
                continue

        else:
            c   = candles[i]
            bar = i - entry_bar

            # Notfall-SL
            emerg_hit = (direction == "long" and c["low"] <= emerg_sl) or \
                        (direction == "short" and c["high"] >= emerg_sl)
            if emerg_hit:
                results.append(-3.0 - COST_R / sl_pct)
                in_trade = False
                continue

            # Time-Stop nach 18 Bars
            if bar >= V3_BARS:
                exit_price = c["close"]
                if direction == "long":
                    pnl_pct = (exit_price - entry) / entry
                else:
                    pnl_pct = (entry - exit_price) / entry
                results.append(_to_r(pnl_pct, sl_pct))
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
    data_dir = os.path.join(PROJECT_DIR, "data", "historical")

    all_v1, all_v2, all_v3 = [], [], []

    print("═" * 72)
    print("  EXIT MATRIX SCOUT — TFR Entry (EMA20×EMA55) + 3 Exit-Varianten")
    print("  V1: TP=1.0R / SL=1.0R  │  V2: TP=1.5R / SL=1.0R  │  V3: Time-Stop 18×4H")
    print("  SL-Definition: 1.5×ATR(14) vom Entry-Close")
    print("═" * 72)

    for asset in ASSETS:
        raw      = load_csv(asset, "15m")
        candles  = aggregate_4h(raw)

        v1_long  = run_v1(candles, "long")
        v1_short = run_v1(candles, "short")
        v2_long  = run_v2(candles, "long")
        v2_short = run_v2(candles, "short")
        v3_long  = run_v3(candles, "long")
        v3_short = run_v3(candles, "short")

        v1 = v1_long + v1_short
        v2 = v2_long + v2_short
        v3 = v3_long + v3_short

        all_v1 += v1
        all_v2 += v2
        all_v3 += v3

        print(f"\n  {asset}")
        _print_row("V1 (1R:1R) LONG",  kpis(v1_long))
        _print_row("V1 (1R:1R) SHORT", kpis(v1_short))
        _print_row("V2 (1.5R:1R) LONG",  kpis(v2_long))
        _print_row("V2 (1.5R:1R) SHORT", kpis(v2_short))
        _print_row("V3 (18-Bar) LONG",  kpis(v3_long))
        _print_row("V3 (18-Bar) SHORT", kpis(v3_short))

    # Portfolio
    print("\n" + "═" * 72)
    print("  PORTFOLIO GESAMT — ALLE 10 ASSETS")
    print("─" * 72)

    k1 = kpis(all_v1)
    k2 = kpis(all_v2)
    k3 = kpis(all_v3)

    _print_row("V1 (TP=1R, SL=1R)",   k1)
    _print_row("V2 (TP=1.5R, SL=1R)", k2)
    _print_row("V3 (18-Bar Exit)",     k3)

    print("\n")
    print("  V1 R-Verteilung:")
    print(ascii_dist(all_v1))
    print("  V2 R-Verteilung:")
    print(ascii_dist(all_v2))
    print("  V3 R-Verteilung:")
    print(ascii_dist(all_v3))

    print("═" * 72)
    print("  SIEGER-ANALYSE")
    best = max([(k1["avg_r"], "V1"), (k2["avg_r"], "V2"), (k3["avg_r"], "V3")],
               key=lambda x: x[0])
    print(f"  Beste AvgR: {best[1]}  ({best[0]:+.4f}R)")

    # Entscheidungsmatrix
    print("\n  Entscheidungsmatrix:")
    print(f"  {'Variante':<22} {'AvgR':>8} {'WR':>7} {'PF':>6} {'p':>8}  {'Entscheidung'}")
    print("  " + "─" * 65)
    for label, k in [("V1 (TP=1R, SL=1R)", k1), ("V2 (TP=1.5R, SL=1R)", k2), ("V3 (18-Bar)", k3)]:
        gate = "✅ GO-Kandidat" if k["avg_r"] > 0 and k["p"] < 0.05 else \
               "⚠️  pos. / n.s." if k["avg_r"] > 0 else "❌ negativ"
        print(f"  {label:<22} {k['avg_r']:>+8.4f}R {k['wr']*100:>6.1f}% {k['pf']:>6.2f} {k['p']:>8.4f}  {gate}")

    print("═" * 72)


if __name__ == "__main__":
    main()
