#!/usr/bin/env python3
"""
Donchian Channel Trend-Following Scout — 4H-Timeframe.

Entry  Long : 4H-Close > höchstes High der letzten 20 Perioden → LONG
Entry  Short: 4H-Close < tiefstes Low  der letzten 20 Perioden → SHORT
Exit        : Close unter/über 10-Perioden-Trailing-Low/High
Initial SL  : 10-Perioden-Low (Long) / 10-Perioden-High (Short)
Kein fester TP — Fat-Tails erwünscht.

Verwendung:
  python3 scripts/backtest/donchian_scout.py
  python3 scripts/backtest/donchian_scout.py --assets BTC,ETH,SOL --dc 20 --trail 10
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, aggregate_4h, TAKER_FEE, SLIPPAGE

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
DC_PERIOD      = 20   # Donchian-Breakout-Fenster
TRAIL_PERIOD   = 10   # Trailing-Stop-Fenster
MAX_HOLD       = 500  # Sicherheits-Timeout (4H-Candles, ~83 Tage)


def donchian(candles: list[dict], period: int, idx: int) -> tuple[float, float]:
    """Höchstes High / tiefstes Low der letzten `period` Candles vor idx."""
    window = candles[max(0, idx - period): idx]
    if not window:
        return 0.0, float("inf")
    return max(c["high"] for c in window), min(c["low"] for c in window)


def run_scout(assets: list[str], start: str, end: str,
              dc_period: int, trail_period: int) -> list[dict]:
    trades = []
    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            continue
        c4h = aggregate_4h(candles_15m)

        in_trade = False
        trade = {}
        n_asset = 0

        for i, candle in enumerate(c4h):
            dt  = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")

            # ── Offene Position managen ───────────────────────────────────────
            if in_trade:
                direction = trade["direction"]
                dc_hi, dc_lo = donchian(c4h, trail_period, i)
                trail_sl = dc_lo if direction == "long" else dc_hi

                # Trailing SL nur in Profit-Richtung bewegen
                if direction == "long":
                    trade["trail_sl"] = max(trade["trail_sl"], trail_sl)
                else:
                    trade["trail_sl"] = min(trade["trail_sl"], trail_sl)

                # Exit: Close unter/über Trailing-SL
                exit_triggered = (
                    (direction == "long"  and candle["close"] < trade["trail_sl"]) or
                    (direction == "short" and candle["close"] > trade["trail_sl"])
                )
                timeout = (i - trade["entry_idx"]) >= MAX_HOLD

                if exit_triggered or timeout:
                    exit_price  = candle["close"]
                    actual_exit = exit_price * (1 - SLIPPAGE) if direction == "long" \
                                  else exit_price * (1 + SLIPPAGE)
                    fee_exit    = actual_exit * TAKER_FEE

                    risk = trade["risk"]
                    ae   = trade["actual_entry"]
                    if direction == "long":
                        gross_r = (actual_exit - ae) / risk
                    else:
                        gross_r = (ae - actual_exit) / risk

                    fee_total = (trade["fee_entry"] + fee_exit) / risk
                    net_r = gross_r - fee_total

                    n_candles = i - trade["entry_idx"]
                    trade.update({
                        "exit_price":   round(exit_price, 6),
                        "exit_day":     day,
                        "net_r":        round(net_r, 4),
                        "gross_r":      round(gross_r, 4),
                        "n_candles":    n_candles,
                        "hold_days":    round(n_candles * 4 / 24, 1),
                        "exit_reason":  "timeout" if timeout else "trail_sl",
                    })
                    trades.append(trade)
                    n_asset += 1
                    in_trade = False
                    trade = {}
                continue

            # ── Kein offener Trade: Entry-Check ──────────────────────────────
            if i < dc_period + trail_period or day < start or day > end:
                continue

            dc_hi_entry, dc_lo_entry = donchian(c4h, dc_period, i)
            _, trail_lo = donchian(c4h, trail_period, i)
            trail_hi, _ = donchian(c4h, trail_period, i)

            direction = None
            if candle["close"] > dc_hi_entry:
                direction = "long"
            elif candle["close"] < dc_lo_entry:
                direction = "short"
            if direction is None:
                continue

            entry = candle["close"]
            if direction == "long":
                sl = trail_lo   # initialer SL = 10-Perioden-Low
            else:
                sl = trail_hi   # initialer SL = 10-Perioden-High

            risk = abs(entry - sl)
            if risk <= 0 or risk / entry < 0.001 or risk / entry > 0.40:
                continue

            actual_entry = entry * (1 + SLIPPAGE) if direction == "long" \
                           else entry * (1 - SLIPPAGE)
            fee_entry = actual_entry * TAKER_FEE

            in_trade = True
            trade = {
                "asset":        asset,
                "direction":    direction,
                "entry_day":    day,
                "entry_idx":    i,
                "entry_price":  round(entry, 6),
                "actual_entry": actual_entry,
                "sl_initial":   round(sl, 6),
                "trail_sl":     sl,
                "risk":         risk,
                "fee_entry":    fee_entry,
                "dc_level":     round(dc_hi_entry if direction == "long" else dc_lo_entry, 6),
            }

        print(f"   {asset:<5}: {n_asset} Trades")

    return trades


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
    t_stat = mean / (sd / math.sqrt(n)) if sd > 0 else 0

    def erfc(x):
        t = 1.0 / (1.0 + 0.3275911 * abs(x))
        p = t*(0.254829592+t*(-0.284496736+t*(1.421413741+t*(-1.453152027+t*1.061405429))))
        return p * math.exp(-x * x)

    p_val = erfc(abs(t_stat) / math.sqrt(2)) if t_stat != 0 else 1.0
    return {
        "n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
        "pf": gw/gl if gl > 0 else float("inf"),
        "sharpe": mean/sd if sd > 0 else 0,
        "max_dd": dd, "t": t_stat, "p": p_val,
        "best_r": max(r_list), "worst_r": min(r_list),
        "avg_hold": 0,
    }


def ascii_dist(r_list: list[float], bins: int = 20) -> str:
    """Einfache ASCII-Verteilung der R-Ergebnisse."""
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
        line = ""
        for c in counts:
            line += "█" if c / max_c >= row / height else " "
        lines.append("  |" + line)
    lines.append(f"  |{'─'*bins}")
    lines.append(f"  {lo:>+6.1f}R{'':>{bins-12}}{hi:>+6.1f}R")
    return "\n".join(lines)


def print_results(trades: list[dict], dc_period: int, trail_period: int):
    rs = [t["net_r"] for t in trades]
    k  = kpis(rs)

    print(f"\n  ═══ Donchian {dc_period}/{trail_period} — Gesamt (n={k['n']}) ═══")
    if k["n"] == 0:
        print("  Keine Trades."); return

    pf = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
    print(f"  Win-Rate    : {k['wr']*100:.1f}%")
    print(f"  Avg R       : {k['avg_r']:>+8.4f}R")
    print(f"  Total R     : {k['total_r']:>+8.2f}R")
    print(f"  Profit Fakt.: {pf}")
    print(f"  Sharpe      : {k['sharpe']:>+8.3f}")
    print(f"  Max DD      : {k['max_dd']:>8.2f}R")
    print(f"  Best Trade  : {k['best_r']:>+8.2f}R")
    print(f"  Worst Trade : {k['worst_r']:>+8.2f}R")
    print(f"  t-Statistik : {k['t']:>+8.3f}  p={k['p']:.4f}")
    avg_hold = sum(t["hold_days"] for t in trades) / len(trades)
    print(f"  Ø Haltedauer: {avg_hold:.1f} Tage")

    # Richtungs-Split
    for dir_label, direction in [("LONG", "long"), ("SHORT", "short")]:
        sub = [t["net_r"] for t in trades if t["direction"] == direction]
        if not sub: continue
        ks = kpis(sub)
        print(f"  → {dir_label} (n={len(sub):>3}): "
              f"AvgR={ks['avg_r']:>+7.4f}R  WR={ks['wr']*100:.1f}%  "
              f"Total={ks['total_r']:>+7.2f}R  p={ks['p']:.4f}")

    # Cross-Asset
    print(f"\n  Cross-Asset:")
    assets = sorted(set(t["asset"] for t in trades))
    pos = 0
    for asset in assets:
        sub = [t["net_r"] for t in trades if t["asset"] == asset]
        ks  = kpis(sub)
        icon = "✅" if ks["avg_r"] > 0 else "❌"
        if ks["avg_r"] > 0: pos += 1
        print(f"    {icon} {asset:<5}: n={len(sub):>2}  "
              f"AvgR={ks['avg_r']:>+7.4f}R  WR={ks['wr']*100:.0f}%  "
              f"Best={ks['best_r']:>+6.2f}R  Worst={ks['worst_r']:>+6.2f}R")
    print(f"  Positive Assets: {pos}/{len(assets)}")

    # Top 5 Trades
    top5 = sorted(trades, key=lambda t: t["net_r"], reverse=True)[:5]
    print(f"\n  Top-5 Trades:")
    for t in top5:
        print(f"    {t['asset']:<5} {t['direction']:<5} {t['entry_day']} → {t['exit_day']}"
              f"  {t['net_r']:>+7.2f}R  ({t['hold_days']:.1f}d)  [{t['exit_reason']}]")

    # R-Verteilung
    print(f"\n  R-Verteilung:")
    print(ascii_dist(rs))

    # Entscheidung
    print(f"\n  ═══ Scout-Entscheidung ═══")
    if k["avg_r"] > 0.10 and k["p"] < 0.05:
        print(f"  ✅ SIGNAL — Avg R={k['avg_r']:>+.4f}R, p={k['p']:.4f} → Vollständiger Zyklus empfohlen")
    elif k["avg_r"] > 0:
        print(f"  ⚠️  SCHWACH — Avg R={k['avg_r']:>+.4f}R positiv aber p={k['p']:.4f}")
    else:
        print(f"  ❌ KEIN SIGNAL — Avg R={k['avg_r']:>+.4f}R negativ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets",  default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",    dest="start", default="2025-04-21")
    parser.add_argument("--to",      dest="end",   default="2026-04-19")
    parser.add_argument("--dc",      type=int, default=DC_PERIOD,    help="Donchian-Breakout-Periode")
    parser.add_argument("--trail",   type=int, default=TRAIL_PERIOD, help="Trailing-Stop-Periode")
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    print(f"🐢 Donchian Channel Trend-Following")
    print(f"   Assets  : {', '.join(assets)}")
    print(f"   Periode : {args.start} → {args.end}")
    print(f"   DC({args.dc}) Breakout / Trail({args.trail}) Exit — 4H-Candles")
    print()

    trades = run_scout(assets, args.start, args.end, args.dc, args.trail)
    print(f"\n   Gesamt Trades: {len(trades)}")

    print_results(trades, args.dc, args.trail)


if __name__ == "__main__":
    main()
