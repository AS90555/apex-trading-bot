#!/usr/bin/env python3
"""
PDC Scout — PDH/PDL Chandelier
===============================
Hypothese: Das Vortageshoch/-tief (PDH/PDL) ist ein echter institutioneller
Liquiditätslevel. Beim erstmaligen Schlusskurs-Bruch mit Volumen-Confirmation
(1H-Kerze) leitet er gerichtete Bewegungen ein, die der Chandelier Trailing Stop
vollständig einfängt — ohne TP-Kappung.

Signal-Logik (1H):
  LONG:  Close > PDH des Vortages  +  erster Bruch des Tages  +  Vol > 1.2×SMA(50)
  SHORT: Close < PDL des Vortages  +  erster Bruch des Tages  +  Vol > 1.2×SMA(50)

Entry:  Market-Close der Ausbruchskerze
Exit:   Chandelier Trailing Stop (2.5×ATR(14), nie gegen den Trade ziehen)

R-Metrik: (Exit − Entry) / Initial_Risk  wobei Initial_Risk = 2.5×ATR@Entry

Adressiert das PDH/PDL-Timeout-Problem (TP=2R nie erreicht) durch unbegrenzten Exit
und das Donchian-WR-Problem durch selektivere Makro-Level.

Usage:
  venv/bin/python3 scripts/backtest/pdc_scout.py
  venv/bin/python3 scripts/backtest/pdc_scout.py --assets ETH,SOL --dir long
  venv/bin/python3 scripts/backtest/pdc_scout.py --from 2026-01-01
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone, timedelta

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv

TAKER_FEE = 0.0006
SLIPPAGE  = 0.0005

# ─── Parameter ────────────────────────────────────────────────────────────────
ATR_PERIOD = 14
ATR_MULT   = 2.5    # Chandelier-Faktor (identisch zu ATR-Rider)
VOL_PERIOD = 50
VOL_MULT   = 1.2

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = ATR_PERIOD + VOL_PERIOD + 5


# ─── 1H-Aggregation ───────────────────────────────────────────────────────────

def aggregate_1h(candles_15m: list[dict]) -> list[dict]:
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


def build_pdh_pdl(candles_15m: list[dict]) -> dict[str, dict]:
    """
    Berechnet PDH/PDL pro Kalendertag (UTC).
    Gibt {date_str: {"pdh": float, "pdl": float}} für den NÄCHSTEN Tag zurück,
    damit bar i den PDH/PDL des VORTAGES abrufen kann (kein Look-Ahead).
    """
    daily: dict[str, dict] = {}
    for c in candles_15m:
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        if day not in daily:
            daily[day] = {"high": c["high"], "low": c["low"]}
        else:
            daily[day]["high"] = max(daily[day]["high"], c["high"])
            daily[day]["low"]  = min(daily[day]["low"],  c["low"])

    # Jeder Handels-Tag bekommt PDH/PDL des Vortages
    levels: dict[str, dict] = {}
    sorted_days = sorted(daily.keys())
    for i in range(1, len(sorted_days)):
        today      = sorted_days[i]
        yesterday  = sorted_days[i - 1]
        levels[today] = {
            "pdh": daily[yesterday]["high"],
            "pdl": daily[yesterday]["low"],
        }
    return levels


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


# ─── KPIs (rechts-schiefe Verteilung) ────────────────────────────────────────

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
        sharpe = (avg_r / std) * math.sqrt(8760)
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

def run_pdc(candles_1h: list[dict], pdh_pdl: dict[str, dict],
            direction: str = "both") -> dict:
    """
    Backtestet PDC-Strategie auf 1H-Candles.
    pdh_pdl: {date_str: {"pdh": float, "pdl": float}}
    """
    atr_s   = _atr_series(candles_1h, ATR_PERIOD)
    vol_sma = _sma_series([c["volume"] for c in candles_1h], VOL_PERIOD)

    longs:  list[float] = []
    shorts: list[float] = []
    trade:  dict | None = None

    # Tracking: erster Bruch pro Richtung pro Tag
    crossed_today: dict[str, set] = {}   # {date_str: {"long", "short"}}

    for i in range(WARMUP, len(candles_1h)):
        c    = candles_1h[i]
        atr  = atr_s[i]
        vsma = vol_sma[i]

        if math.isnan(atr) or math.isnan(vsma):
            continue

        dt      = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day_str = dt.strftime("%Y-%m-%d")
        levels  = pdh_pdl.get(day_str)
        if levels is None:
            continue

        pdh = levels["pdh"]
        pdl = levels["pdl"]

        if day_str not in crossed_today:
            crossed_today[day_str] = set()

        # ── 1. Offenen Trade managen ──────────────────────────────────────────
        if trade is not None:
            if trade["side"] == "long":
                if c["high"] > trade["peak"]:
                    trade["peak"] = c["high"]
                new_sl = trade["peak"] - ATR_MULT * atr
                trade["sl"] = max(trade["sl"], new_sl)
                if c["low"] <= trade["sl"]:
                    exit_p = trade["sl"]
                    r_gross = (exit_p - trade["entry"]) / trade["initial_risk"]
                    fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    longs.append(round(r_gross - fees_r, 3))
                    trade = None
            else:  # short
                if c["low"] < trade["peak"]:
                    trade["peak"] = c["low"]
                new_sl = trade["peak"] + ATR_MULT * atr
                trade["sl"] = min(trade["sl"], new_sl)
                if c["high"] >= trade["sl"]:
                    exit_p = trade["sl"]
                    r_gross = (trade["entry"] - exit_p) / trade["initial_risk"]
                    fees_r  = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["initial_risk"]
                    shorts.append(round(r_gross - fees_r, 3))
                    trade = None

            if trade is not None:
                continue   # Trade läuft noch — kein neuer Entry

        # ── 2. Signal-Erkennung ───────────────────────────────────────────────
        vol_ok = c["volume"] > VOL_MULT * vsma
        if not vol_ok:
            continue

        initial_risk = ATR_MULT * atr
        if initial_risk < 1e-6:
            continue

        # LONG: Close > PDH, erster Bruch heute
        if direction in ("long", "both"):
            if "long" not in crossed_today[day_str] and c["close"] > pdh:
                crossed_today[day_str].add("long")
                entry = c["close"]
                sl    = entry - initial_risk
                trade = {
                    "side": "long", "entry": entry, "sl": sl,
                    "peak": entry, "initial_risk": initial_risk,
                }
                continue   # kein Short-Check wenn Long offen

        # SHORT: Close < PDL, erster Bruch heute
        if direction in ("short", "both"):
            if "short" not in crossed_today[day_str] and c["close"] < pdl:
                crossed_today[day_str].add("short")
                entry = c["close"]
                sl    = entry + initial_risk
                trade = {
                    "side": "short", "entry": entry, "sl": sl,
                    "peak": entry, "initial_risk": initial_risk,
                }

    return {"long": longs, "short": shorts}


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(asset: str, result: dict) -> None:
    for side, r_list in [("LONG", result["long"]), ("SHORT", result["short"])]:
        k = kpis(r_list)
        print(f"\n{'─'*60}")
        print(f"  {asset} {side}  │  n={k['n']}  AvgR={k['avg_r']:+.4f}R  "
              f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")
        print(f"  Expectancy={k['expectancy']:+.4f}R  "
              f"AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R")
        print(f"  P10={k['p10']:+.2f}R  P50={k['p50']:+.2f}R  "
              f"P90={k['p90']:+.2f}R  Max={k['max_r']:+.2f}R")
        print(f"  TotalR={k['total_r']:+.1f}R  MaxDD={k['max_dd']:.2f}R  "
              f"Sharpe(ann)={k['sharpe']:.2f}  t={k['t']:.3f}")
        if r_list:
            print(ascii_dist(r_list))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(ASSETS))
    parser.add_argument("--dir",    default="both", choices=["long", "short", "both"])
    parser.add_argument("--from",   dest="from_date", default=None,
                        help="Startdatum YYYY-MM-DD (UTC)")
    args = parser.parse_args()

    assets = [a.strip().upper() for a in args.assets.split(",")]
    from_ts = None
    if args.from_date:
        from_ts = int(datetime.strptime(args.from_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp() * 1000)

    all_longs:  list[float] = []
    all_shorts: list[float] = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            print(f"[SKIP] {asset}: keine 15m-Daten")
            continue

        if from_ts:
            candles_15m = [c for c in candles_15m if c["time"] >= from_ts]

        if len(candles_15m) < 200:
            print(f"[SKIP] {asset}: zu wenig Daten ({len(candles_15m)} Bars)")
            continue

        pdh_pdl    = build_pdh_pdl(candles_15m)
        candles_1h = aggregate_1h(candles_15m)
        result     = run_pdc(candles_1h, pdh_pdl, direction=args.dir)

        print_report(asset, result)
        all_longs.extend(result["long"])
        all_shorts.extend(result["short"])

    if len(assets) > 1:
        print(f"\n{'═'*60}")
        print("  PORTFOLIO GESAMT")
        for side, r_list in [("LONG", all_longs), ("SHORT", all_shorts)]:
            k = kpis(r_list)
            print(f"\n  {side}  n={k['n']}  AvgR={k['avg_r']:+.4f}R  "
                  f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")
            print(f"  Expectancy={k['expectancy']:+.4f}R  "
                  f"AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R")
            print(f"  P10={k['p10']:+.2f}R  P50={k['p50']:+.2f}R  "
                  f"P90={k['p90']:+.2f}R  Max={k['max_r']:+.2f}R")
            print(f"  TotalR={k['total_r']:+.1f}R  MaxDD={k['max_dd']:.2f}R  "
                  f"Sharpe(ann)={k['sharpe']:.2f}  t={k['t']:.3f}")
            if r_list:
                print(ascii_dist(r_list))

        combined = all_longs + all_shorts
        if combined:
            k = kpis(combined)
            print(f"\n  LONG+SHORT COMBINED  n={k['n']}  AvgR={k['avg_r']:+.4f}R  "
                  f"WR={k['wr']:.1%}  PF={k['pf']:.2f}  p={k['p']:.4f}")


if __name__ == "__main__":
    main()
