#!/usr/bin/env python3
"""
State Scout — TFR vs AVT: State-Driven Trend Following
=======================================================
Paradigmenwechsel von Moment-Signalen (Breakout = eine Bar) zu State-Signalen
(Trend-Zustand = viele Bars konsistenter Richtung).

Kandidat 1 — TFR (Trend Following Rotation):
  Signal:  EMA(20) kreuzt EMA(55) auf 4H-Chart
  LONG:    ema20[i] > ema55[i] AND ema20[i-1] <= ema55[i-1]
  SHORT:   ema20[i] < ema55[i] AND ema20[i-1] >= ema55[i-1]
  Entry:   Market-Close der Crossover-Kerze
  Exit:    Chandelier Trailing Stop (2.5×ATR(14))

Kandidat 2 — AVT (Anchored VWAP Trend):
  VWAP:    Weekly Anchored VWAP (Reset jeden Montag 00:00 UTC)
           VWAP = Σ(Vol × TypicalPrice) / Σ(Vol)
  LONG:    Close kreuzt von unter nach über Weekly-VWAP (erstes Crossing/Richtung/Woche)
  SHORT:   Close kreuzt von über nach unter Weekly-VWAP (erstes Crossing/Richtung/Woche)
  Entry:   Market-Close der Crossover-Kerze
  Exit:    Chandelier Trailing Stop (2.5×ATR(14))

Gemeinsam:
  - 4H-Candles aus 15m-CSV aggregiert
  - Kein fixer TP — Trend läuft bis Chandelier stoppt
  - Fees + Slippage identisch zu allen anderen Scouts

Usage:
  venv/bin/python3 scripts/backtest/state_scout.py
  venv/bin/python3 scripts/backtest/state_scout.py --strategy tfr --dir long
  venv/bin/python3 scripts/backtest/state_scout.py --strategy avt --assets ETH,SOL
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
# TFR
EMA_FAST   = 20
EMA_SLOW   = 55
# AVT & gemeinsam
ATR_PERIOD = 14
ATR_MULT   = 2.5

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = EMA_SLOW + ATR_PERIOD + 10


# ─── Indikator-Serien ─────────────────────────────────────────────────────────

def _ema_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    if len(values) < period:
        return out
    sma = sum(values[:period]) / period
    out[period - 1] = sma
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


def _weekly_vwap_series(candles: list[dict]) -> list[float]:
    """
    Weekly Anchored VWAP — Reset jeden Montag 00:00 UTC (ISO-Woche).
    VWAP[i] = Σ(Vol × TypPrice) / Σ(Vol) seit letztem Monday-Reset.
    Kein Look-Ahead: VWAP[i] nutzt nur Bars 0..i.
    """
    out = [float("nan")] * len(candles)
    cum_vp = 0.0
    cum_v  = 0.0
    cur_week: tuple | None = None

    for i, c in enumerate(candles):
        dt        = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        week_key  = dt.isocalendar()[:2]   # (year, iso_week_number)

        if week_key != cur_week:
            # Neue Woche — VWAP reset
            cum_vp   = 0.0
            cum_v    = 0.0
            cur_week = week_key

        typ_price = (c["high"] + c["low"] + c["close"]) / 3
        cum_vp   += c["volume"] * typ_price
        cum_v    += c["volume"]

        if cum_v > 0:
            out[i] = cum_vp / cum_v

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
        sharpe = (avg_r / std) * math.sqrt(2190)
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


def ascii_dist(r_list: list[float], bins: int = 20) -> str:
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
        bar = "█" * int(c / max_c * 24)
        lines.append(f"  {lo + i*width:+7.2f}R │{bar} ({c})")
    return "\n".join(lines)


# ─── Chandelier-Exit (gemeinsam für TFR und AVT) ──────────────────────────────

def _chandelier_manage(trade: dict, c: dict, atr: float) -> tuple[dict | None, float | None]:
    """
    Managt einen offenen Chandelier-Trade.
    Gibt (None, r) zurück wenn Trade geschlossen, sonst (trade_updated, None).
    """
    if trade["side"] == "long":
        if c["high"] > trade["peak"]:
            trade["peak"] = c["high"]
        new_sl = trade["peak"] - ATR_MULT * atr
        trade["sl"] = max(trade["sl"], new_sl)
        if c["low"] <= trade["sl"]:
            r_gross = (trade["sl"] - trade["entry"]) / trade["initial_risk"]
            fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
            return None, round(r_gross - fees_r, 3)
    else:  # short
        if c["low"] < trade["peak"]:
            trade["peak"] = c["low"]
        new_sl = trade["peak"] + ATR_MULT * atr
        trade["sl"] = min(trade["sl"], new_sl)
        if c["high"] >= trade["sl"]:
            r_gross = (trade["entry"] - trade["sl"]) / trade["initial_risk"]
            fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
            return None, round(r_gross - fees_r, 3)
    return trade, None


def _open_trade(side: str, entry: float, atr: float) -> dict | None:
    initial_risk = ATR_MULT * atr
    if initial_risk < 1e-6:
        return None
    sl = entry - initial_risk if side == "long" else entry + initial_risk
    return {"side": side, "entry": entry, "sl": sl, "peak": entry,
            "initial_risk": initial_risk}


# ─── TFR: EMA-Crossover Backtest ──────────────────────────────────────────────

def run_tfr(candles: list[dict], direction: str = "both") -> dict:
    closes  = [c["close"] for c in candles]
    ema20_s = _ema_series(closes, EMA_FAST)
    ema55_s = _ema_series(closes, EMA_SLOW)
    atr_s   = _atr_series(candles, ATR_PERIOD)

    longs: list[float] = []
    shorts: list[float] = []
    trade:  dict | None = None

    for i in range(WARMUP, len(candles)):
        c    = candles[i]
        atr  = atr_s[i]
        e20  = ema20_s[i];  e20p = ema20_s[i-1]
        e55  = ema55_s[i];  e55p = ema55_s[i-1]

        if math.isnan(atr) or math.isnan(e20) or math.isnan(e55): continue
        if math.isnan(e20p) or math.isnan(e55p): continue

        # Offenen Trade managen
        if trade is not None:
            trade, r = _chandelier_manage(trade, c, atr)
            if r is not None:
                (longs if trade is None and r is not None and
                 shorts.append(r) is None else longs).append(r) \
                    if False else (shorts if trade is None else None)
                # Cleaner version:
            if r is not None:
                if trade is None:
                    # side war gespeichert bevor Chandelier-Manage
                    pass
            continue  # Placeholder — see below

        if trade is not None:
            continue

    # Saubere Implementierung ohne verschachtelte Logik
    longs.clear(); shorts.clear(); trade = None

    for i in range(WARMUP, len(candles)):
        c    = candles[i]
        atr  = atr_s[i]
        e20  = ema20_s[i];  e20p = ema20_s[i-1]
        e55  = ema55_s[i];  e55p = ema55_s[i-1]

        if math.isnan(atr) or math.isnan(e20) or math.isnan(e55): continue
        if math.isnan(e20p) or math.isnan(e55p): continue

        if trade is not None:
            prev_side = trade["side"]
            trade, r = _chandelier_manage(trade, c, atr)
            if r is not None:
                (longs if prev_side == "long" else shorts).append(r)
            if trade is not None:
                continue

        # EMA-Crossover Signal
        bullish_cross = e20p <= e55p and e20 > e55
        bearish_cross = e20p >= e55p and e20 < e55

        if direction in ("long", "both") and bullish_cross:
            trade = _open_trade("long", c["close"], atr)
        elif direction in ("short", "both") and bearish_cross:
            trade = _open_trade("short", c["close"], atr)

    return {"long": longs, "short": shorts}


# ─── AVT: Weekly Anchored VWAP Backtest ───────────────────────────────────────

def run_avt(candles: list[dict], direction: str = "both") -> dict:
    vwap_s = _weekly_vwap_series(candles)
    atr_s  = _atr_series(candles, ATR_PERIOD)

    longs: list[float] = []
    shorts: list[float] = []
    trade:  dict | None = None

    # Tracking: erstes Crossing pro Richtung pro Woche
    crossed_this_week: set[str] = set()
    cur_week: tuple | None = None

    for i in range(WARMUP, len(candles)):
        c      = candles[i]
        atr    = atr_s[i]
        vwap   = vwap_s[i]
        vwap_p = vwap_s[i-1] if i > 0 else float("nan")

        if math.isnan(atr) or math.isnan(vwap) or math.isnan(vwap_p): continue

        # Wochenreset für Crossing-Tracker
        dt       = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        week_key = dt.isocalendar()[:2]
        if week_key != cur_week:
            crossed_this_week = set()
            cur_week = week_key

        # Offenen Trade managen
        if trade is not None:
            prev_side = trade["side"]
            trade, r  = _chandelier_manage(trade, c, atr)
            if r is not None:
                (longs if prev_side == "long" else shorts).append(r)
            if trade is not None:
                continue

        prev_close = candles[i-1]["close"]

        # Bullish Cross: vorheriger Close unter VWAP, aktueller Close über VWAP
        bullish = prev_close < vwap_p and c["close"] > vwap
        # Bearish Cross: vorheriger Close über VWAP, aktueller Close unter VWAP
        bearish = prev_close > vwap_p and c["close"] < vwap

        if direction in ("long", "both") and bullish and "long" not in crossed_this_week:
            t = _open_trade("long", c["close"], atr)
            if t:
                trade = t
                crossed_this_week.add("long")

        elif direction in ("short", "both") and bearish and "short" not in crossed_this_week:
            t = _open_trade("short", c["close"], atr)
            if t:
                trade = t
                crossed_this_week.add("short")

    return {"long": longs, "short": shorts}


# ─── Report & Vergleich ───────────────────────────────────────────────────────

def print_kpi_line(label: str, r_list: list[float]) -> None:
    k = kpis(r_list)
    print(f"  {label:<22} │  n={k['n']:>4}  AvgR={k['avg_r']:+.4f}R  "
          f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")
    if k["n"] > 0:
        print(f"  {'':22}    Exp={k['expectancy']:+.4f}R  "
              f"AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R  "
              f"Max={k['max_r']:+.2f}R")


def print_dist(label: str, r_list: list[float]) -> None:
    if not r_list: return
    k = kpis(r_list)
    print(f"\n{'─'*56}")
    print(f"  {label}  P10={k['p10']:+.2f}R P50={k['p50']:+.2f}R P90={k['p90']:+.2f}R")
    print(ascii_dist(r_list))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets",   default=",".join(ASSETS))
    parser.add_argument("--strategy", default="both", choices=["tfr", "avt", "both"])
    parser.add_argument("--dir",      default="both", choices=["long", "short", "both"])
    parser.add_argument("--from",     dest="from_date", default=None)
    args = parser.parse_args()

    assets  = [a.strip().upper() for a in args.assets.split(",")]
    from_ts = None
    if args.from_date:
        from_ts = int(datetime.strptime(args.from_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp() * 1000)

    print(f"{'═'*60}")
    print(f"  STATE SCOUT — TFR vs AVT  │  {args.dir.upper()}  │  4H")
    print(f"  TFR: EMA({EMA_FAST}) × EMA({EMA_SLOW}) Crossover")
    print(f"  AVT: Weekly Anchored VWAP (Reset Mo 00:00 UTC)")
    print(f"  Exit: Chandelier {ATR_MULT}×ATR({ATR_PERIOD})  │  Assets: {', '.join(assets)}")
    print(f"{'═'*60}")

    tfr_longs: list[float] = []
    tfr_shorts: list[float] = []
    avt_longs: list[float] = []
    avt_shorts: list[float] = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            continue
        if from_ts:
            candles_15m = [c for c in candles_15m if c["time"] >= from_ts]
        candles_4h = aggregate_4h(candles_15m)
        if len(candles_4h) < WARMUP + 20:
            print(f"\n[SKIP] {asset}: nur {len(candles_4h)} 4H-Bars")
            continue

        print(f"\n  {asset}")
        if args.strategy in ("tfr", "both"):
            r = run_tfr(candles_4h, direction=args.dir)
            tfr_longs.extend(r["long"]); tfr_shorts.extend(r["short"])
            print_kpi_line(f"TFR LONG",  r["long"])
            print_kpi_line(f"TFR SHORT", r["short"])

        if args.strategy in ("avt", "both"):
            r = run_avt(candles_4h, direction=args.dir)
            avt_longs.extend(r["long"]); avt_shorts.extend(r["short"])
            print_kpi_line(f"AVT LONG",  r["long"])
            print_kpi_line(f"AVT SHORT", r["short"])

    # ── Portfolio-Vergleich ────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  PORTFOLIO GESAMT — DIREKTVERGLEICH")
    print(f"{'─'*60}")

    if args.strategy in ("tfr", "both"):
        tfr_all = tfr_longs + tfr_shorts
        print_kpi_line("TFR LONG",     tfr_longs)
        print_kpi_line("TFR SHORT",    tfr_shorts)
        print_kpi_line("TFR KOMBINIERT", tfr_all)
        if tfr_all:
            print_dist("TFR KOMBINIERT", tfr_all)

    print()
    if args.strategy in ("avt", "both"):
        avt_all = avt_longs + avt_shorts
        print_kpi_line("AVT LONG",     avt_longs)
        print_kpi_line("AVT SHORT",    avt_shorts)
        print_kpi_line("AVT KOMBINIERT", avt_all)
        if avt_all:
            print_dist("AVT KOMBINIERT", avt_all)

    # ── Sieger ────────────────────────────────────────────────────────────────
    if args.strategy == "both":
        tfr_all = tfr_longs + tfr_shorts
        avt_all = avt_longs + avt_shorts
        tfr_k = kpis(tfr_all)
        avt_k = kpis(avt_all)
        print(f"\n{'═'*60}")
        print("  SIEGER-VERGLEICH")
        print(f"  TFR: AvgR={tfr_k['avg_r']:+.4f}R  WR={tfr_k['wr']:.1%}  "
              f"PF={tfr_k['pf']:.2f}  p={tfr_k['p']:.4f}  n={tfr_k['n']}")
        print(f"  AVT: AvgR={avt_k['avg_r']:+.4f}R  WR={avt_k['wr']:.1%}  "
              f"PF={avt_k['pf']:.2f}  p={avt_k['p']:.4f}  n={avt_k['n']}")
        winner = "TFR" if tfr_k["avg_r"] > avt_k["avg_r"] else "AVT"
        print(f"\n  → Bessere AvgR: {winner}")


if __name__ == "__main__":
    main()
