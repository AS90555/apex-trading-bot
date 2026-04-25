#!/usr/bin/env python3
"""
Liquidation Cascade Reversion Scout — 15m-Timeframe.

Setup (LONG):
  Trigger  : Rote Candle (Close < Open)
             Candle-Range > 3.5 × ATR(14)
             Volumen      > 4.0 × Vol-SMA(50)
  Entry    : Open der NÄCHSTEN Candle (simuliert Market-Order bei Kerzen-Schluss)
  SL       : Absolutes Low der Panik-Kerze − 0.5 × ATR
  TP       : 50%-Rücklauf der Panik-Kerze (Low + 0.5 × (High − Low))
  Time-Stop: 96 Candles = 24h

Auswertung: Gesamt, Richtungs-Split, Cross-Asset, R-Verteilung.

Verwendung:
  python3 scripts/backtest/liq_cascade_scout.py
  python3 scripts/backtest/liq_cascade_scout.py --atr-mult 3.0 --vol-mult 3.0
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE

DEFAULT_ASSETS = ["BTC","ETH","SOL","AVAX","XRP","DOGE","ADA","LINK","SUI","AAVE"]
ATR_PERIOD     = 14
VOL_SMA_PERIOD = 50
TIME_STOP_C    = 96     # 24h auf 15m
SL_ATR_MULT    = 0.5    # SL = Panic-Low − 0.5×ATR


def calc_atr(candles: list[dict], idx: int, period: int = 14) -> float:
    if idx < period + 1:
        return 0.0
    window = candles[idx - period: idx]
    trs = []
    for i in range(1, len(window)):
        h, l, pc = window[i]["high"], window[i]["low"], window[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def calc_vol_sma(candles: list[dict], idx: int, period: int = 50) -> float:
    if idx < period:
        return 0.0
    window = candles[idx - period: idx]
    return sum(c["volume"] for c in window) / len(window)


def fee_adj(r: float, risk: float, entry: float) -> float:
    return r - (2 * entry * TAKER_FEE) / risk


def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean)**2 for r in r_list) / (n - 1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1.0 / (1.0 + 0.3275911 * abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p * math.exp(-x * x)
    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {
        "n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
        "pf": gw/gl if gl > 0 else float("inf"),
        "sharpe": mean/sd if sd > 0 else 0,
        "max_dd": dd, "t": t, "p": p,
        "best": max(r_list), "worst": min(r_list),
    }


def run_scout(assets: list[str], start: str, end: str,
              atr_mult: float, vol_mult: float) -> list[dict]:
    trades = []

    for asset in assets:
        candles = load_csv(asset, "15m")
        if not candles:
            continue

        n_asset = 0
        in_trade = False
        trade = {}

        for i, c in enumerate(candles):
            dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")

            # ── Offene Position managen ───────────────────────────────────────
            if in_trade:
                # Entry war am Open dieser Candle (j = entry_candle + 1)
                if i == trade["entry_idx"]:
                    # Entry-Preis setzen (Open dieser Candle + Slippage)
                    ae = c["open"] * (1 + SLIPPAGE)
                    trade["actual_entry"] = ae
                    trade["entry_price"]  = c["open"]
                    trade["fee_entry"]    = ae * TAKER_FEE
                    continue

                if i < trade["entry_idx"]:
                    continue

                ae   = trade["actual_entry"]
                sl   = trade["sl"]
                tp   = trade["tp"]
                risk = ae - sl  # Long: risk = entry - SL

                # SL zuerst (konservativ)
                if c["low"] <= sl:
                    r = fee_adj(-1.0, risk, ae)
                    trade.update({"net_r": round(r, 4), "exit_reason": "sl",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; n_asset += 1
                    continue

                if c["high"] >= tp:
                    gross_r = (tp - ae) / risk
                    r = fee_adj(gross_r, risk, ae)
                    trade.update({"net_r": round(r, 4), "exit_reason": "tp",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; n_asset += 1
                    continue

                if (i - trade["entry_idx"]) >= TIME_STOP_C:
                    gross_r = (c["close"] - ae) / risk
                    r = fee_adj(gross_r, risk, ae)
                    trade.update({"net_r": round(r, 4), "exit_reason": "timeout",
                                  "exit_day": day, "n_candles": TIME_STOP_C})
                    trades.append(trade); in_trade = False; n_asset += 1
                continue

            # ── Entry-Check ───────────────────────────────────────────────────
            if day < start or day > end:
                continue
            if i < max(ATR_PERIOD, VOL_SMA_PERIOD) + 2:
                continue

            atr     = calc_atr(candles, i)
            vol_sma = calc_vol_sma(candles, i)
            if atr <= 0 or vol_sma <= 0:
                continue

            candle_range = c["high"] - c["low"]
            is_red       = c["close"] < c["open"]
            big_range    = candle_range > atr_mult * atr
            big_volume   = c["volume"] > vol_mult * vol_sma

            if not (is_red and big_range and big_volume):
                continue

            # Setup gefunden — Entry am Open der nächsten Candle
            if i + 1 >= len(candles):
                continue

            tp = c["low"] + 0.5 * candle_range   # 50% Rücklauf
            sl = c["low"] - SL_ATR_MULT * atr     # Panic-Low − 0.5 ATR

            # Vorläufige Risk-Schätzung mit Open der nächsten Candle
            approx_entry = candles[i + 1]["open"]
            approx_risk  = approx_entry - sl
            if approx_risk <= 0 or approx_risk / approx_entry < 0.001:
                continue
            if approx_risk / approx_entry > 0.30:   # SL zu weit (>30%)
                continue
            if tp <= approx_entry:                   # TP unter Entry → skip
                continue

            in_trade = True
            trade = {
                "asset":        asset,
                "direction":    "long",
                "trigger_day":  day,
                "entry_idx":    i + 1,
                "panic_open":   round(c["open"],  6),
                "panic_close":  round(c["close"], 6),
                "panic_high":   round(c["high"],  6),
                "panic_low":    round(c["low"],   6),
                "panic_range_atr": round(candle_range / atr, 2),
                "vol_ratio":    round(c["volume"] / vol_sma, 2),
                "atr":          round(atr, 6),
                "sl":           round(sl, 6),
                "tp":           round(tp, 6),
                # actual_entry wird bei i==entry_idx gesetzt
                "actual_entry": 0.0,
                "entry_price":  0.0,
                "fee_entry":    0.0,
            }

        if in_trade and trade.get("net_r") is None:
            in_trade = False  # offener Trade am Ende der Daten → verwerfen

        print(f"   {asset:<5}: {n_asset} Trades")

    return trades


def ascii_dist(r_list: list[float], bins: int = 24) -> str:
    if not r_list: return ""
    lo, hi = min(r_list), max(r_list)
    rng = max(hi - lo, 0.001)
    counts = [0] * bins
    for r in r_list:
        b = min(int((r - lo) / rng * bins), bins - 1)
        counts[b] += 1
    max_c = max(counts) or 1
    height = 6
    lines = []
    for row in range(height, 0, -1):
        line = "".join("█" if counts[b] / max_c >= row / height else " " for b in range(bins))
        lines.append("  |" + line)
    lines.append(f"  |{'─'*bins}")
    lines.append(f"  {lo:>+6.2f}R{'':>{bins-13}}{hi:>+6.2f}R")
    return "\n".join(lines)


def print_results(trades: list[dict], atr_mult: float, vol_mult: float):
    rs = [t["net_r"] for t in trades]
    k  = kpis(rs)

    print(f"\n  ═══ Liq-Cascade Scout  ATR>{atr_mult}x / Vol>{vol_mult}x  (n={k.get('n',0)}) ═══")
    if not k.get("n"):
        print("  Keine Trades."); return

    pf = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
    sig = "✅" if k["avg_r"] > 0.05 and k["p"] < 0.05 else \
          "⚠️ " if k["avg_r"] > 0 else "❌"

    print(f"  Win-Rate    : {k['wr']*100:.1f}%")
    print(f"  Avg R       : {k['avg_r']:>+8.4f}R  {sig}")
    print(f"  Total R     : {k['total_r']:>+8.2f}R")
    print(f"  Profit Fakt.: {pf}")
    print(f"  Sharpe      : {k['sharpe']:>+8.3f}")
    print(f"  Max DD      : {k['max_dd']:>8.2f}R")
    print(f"  Best / Worst: {k['best']:>+7.2f}R / {k['worst']:>+7.2f}R")
    print(f"  t / p       : {k['t']:>+8.3f} / {k['p']:.4f}")

    # Exit-Gründe
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print(f"  Exits       : " + "  ".join(f"{r}={cnt}({cnt/k['n']*100:.0f}%)"
                                           for r, cnt in sorted(reasons.items(), key=lambda x: -x[1])))

    avg_hold = sum(t["n_candles"] * 15 / 60 for t in trades) / len(trades)
    print(f"  Ø Haltedauer: {avg_hold:.1f}h")

    # Cross-Asset
    print(f"\n  Cross-Asset:")
    assets = sorted(set(t["asset"] for t in trades))
    pos = 0
    for asset in assets:
        sub = [t["net_r"] for t in trades if t["asset"] == asset]
        ks  = kpis(sub)
        icon = "✅" if ks["avg_r"] > 0 else "❌"
        if ks["avg_r"] > 0: pos += 1
        print(f"    {icon} {asset:<5}: n={len(sub):>3}  "
              f"AvgR={ks['avg_r']:>+7.4f}R  WR={ks['wr']*100:.0f}%  "
              f"Best={ks['best']:>+5.2f}R")
    print(f"  Positive Assets: {pos}/{len(assets)}")

    # Top-5
    top5 = sorted(trades, key=lambda t: t["net_r"], reverse=True)[:5]
    print(f"\n  Top-5 Trades:")
    for t in top5:
        print(f"    {t['asset']:<5} {t['trigger_day']}  "
              f"Range={t['panic_range_atr']:.1f}×ATR  Vol={t['vol_ratio']:.1f}×SMA  "
              f"→ {t['net_r']:>+6.2f}R [{t['exit_reason']}]")

    # R-Verteilung
    print(f"\n  R-Verteilung:")
    print(ascii_dist(rs))

    # Entscheidung
    print(f"\n  ═══ Scout-Entscheidung ═══")
    if k["avg_r"] > 0.05 and k["p"] < 0.05:
        print(f"  ✅ SIGNAL — vollständiger Zyklus empfohlen")
    elif k["avg_r"] > 0 and k["p"] < 0.10:
        print(f"  ⚠️  SCHWACHES SIGNAL — weitere Parameter testen")
    elif k["avg_r"] > 0:
        print(f"  ⚠️  POSITIV aber p={k['p']:.3f} — statistisch nicht signifikant")
    else:
        print(f"  ❌ KEIN SIGNAL")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets",   default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",     dest="start", default="2025-04-21")
    parser.add_argument("--to",       dest="end",   default="2026-04-19")
    parser.add_argument("--atr-mult", type=float, default=3.5)
    parser.add_argument("--vol-mult", type=float, default=4.0)
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    print(f"💥 Liquidation Cascade Reversion Scout")
    print(f"   Assets  : {', '.join(assets)}")
    print(f"   Periode : {args.start} → {args.end}")
    print(f"   Filter  : Range > {args.atr_mult}×ATR(14) + Vol > {args.vol_mult}×SMA(50)")
    print(f"   Entry   : Open nächste Candle | SL: Panic-Low − 0.5×ATR | TP: 50%-Rücklauf")
    print()

    trades = run_scout(assets, args.start, args.end, args.atr_mult, args.vol_mult)
    print(f"\n   Gesamt Trades: {len(trades)}")
    if trades:
        print_results(trades, args.atr_mult, args.vol_mult)


if __name__ == "__main__":
    main()
