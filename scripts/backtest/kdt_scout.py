#!/usr/bin/env python3
"""
Kinetic Deceleration Trap (KDT) Scout — 1H-Timeframe.

Hypothese: Wenn 3 aufeinanderfolgende Kerzen in Trendrichtung schrumpfende
Körper UND schrumpfendes Volumen zeigen, versiegt die Kaufkraft/Verkaufskraft
mathematisch. Die Falle schnappt zu sobald der Preis das Extrem der kleinsten
Kerze bricht.

SHORT Setup:
  Kontext : Close > EMA(50)  [kurzfristiger Aufwärtstrend]
  Sequenz : Kerzen [-2], [-1], [-0] alle grün (Close > Open)
  Bremsung: Body[-2] > Body[-1] > Body[-0]  (schrumpfende Körper)
  Vakuum  : Vol[-2]  > Vol[-1]  > Vol[-0]   (austrocknendes Volumen)
  Entry   : Sell-Stop am Low von Kerze[-0]  (gültig 2 Folgekerzen)
  SL      : High von Kerze[-0]
  TP      : 3R (fix)

LONG Setup (symmetrisch):
  Kontext : Close < EMA(50)
  Sequenz : Kerzen [-2], [-1], [-0] alle rot (Close < Open)
  Bremsung: Body[-2] > Body[-1] > Body[-0]
  Vakuum  : Vol[-2]  > Vol[-1]  > Vol[-0]
  Entry   : Buy-Stop am High von Kerze[-0]
  SL      : Low von Kerze[-0]
  TP      : 3R

Verwendung:
  python3 scripts/backtest/kdt_scout.py
  python3 scripts/backtest/kdt_scout.py --assets SOL,ETH,BTC
  python3 scripts/backtest/kdt_scout.py --tp 2.0 --entry-window 3
  python3 scripts/backtest/kdt_scout.py --from 2025-06-01 --to 2026-02-10
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
from scripts.backtest.squeeze_scout  import aggregate_1h

DEFAULT_ASSETS = [
    "SOL", "AVAX", "DOGE", "ADA", "SUI", "AAVE",
    "BTC", "ETH", "XRP", "LINK", "BNB", "INJ",
    "NEAR", "APT", "TIA", "PEPE", "JUP", "SEI", "LDO", "OP", "ARB", "WIF",
]
EMA_PERIOD    = 50
ENTRY_WINDOW  = 2     # Kerzen lang ist Sell/Buy-Stop gültig
TP_R          = 3.0
WARMUP        = EMA_PERIOD + 5


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _ema_series(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def build_indicators(candles: list[dict]) -> list[float]:
    closes = [c["close"] for c in candles]
    return _ema_series(closes, EMA_PERIOD)


# ─── Signal-Detektion ─────────────────────────────────────────────────────────

def detect_signal(candles: list[dict], ema50: list[float], i: int) -> dict | None:
    """
    Gibt Signal-Dict zurück oder None.
    Geprüft auf Index i (Kerze[-0] = candles[i], [-1] = candles[i-1], [-2] = candles[i-2]).
    """
    if i < 2:
        return None

    c0, c1, c2 = candles[i], candles[i - 1], candles[i - 2]
    e = ema50[i]
    if e <= 0:
        return None

    body0 = abs(c0["close"] - c0["open"])
    body1 = abs(c1["close"] - c1["open"])
    body2 = abs(c2["close"] - c2["open"])

    # Mindest-Body damit es kein Doji ist (verhindert Flat-Märkte)
    if body0 <= 0:
        return None

    vol0, vol1, vol2 = c0["volume"], c1["volume"], c2["volume"]
    if vol0 <= 0:
        return None

    # SHORT: 3 grüne Kerzen, schrumpfende Körper + Volumen, Preis > EMA50
    if (c0["close"] > c0["open"] and
            c1["close"] > c1["open"] and
            c2["close"] > c2["open"] and
            body0 < body1 < body2 and
            vol0 < vol1 < vol2 and
            c0["close"] > e):
        sl   = c0["high"]
        stop = c0["low"]
        risk = sl - stop
        if risk <= 0 or risk / stop < 0.0005 or risk / stop > 0.15:
            return None
        return {"direction": "short", "stop": stop, "sl": sl, "risk": risk}

    # LONG: 3 rote Kerzen, schrumpfende Körper + Volumen, Preis < EMA50
    if (c0["close"] < c0["open"] and
            c1["close"] < c1["open"] and
            c2["close"] < c2["open"] and
            body0 < body1 < body2 and
            vol0 < vol1 < vol2 and
            c0["close"] < e):
        sl   = c0["low"]
        stop = c0["high"]
        risk = stop - sl
        if risk <= 0 or risk / stop < 0.0005 or risk / stop > 0.15:
            return None
        return {"direction": "long", "stop": stop, "sl": sl, "risk": risk}

    return None


# ─── Backtest-Kern ────────────────────────────────────────────────────────────

def run_kdt(asset: str, candles_1h: list[dict], start: str, end: str,
            tp_r: float, entry_window: int, direction: str = "both") -> list[dict]:
    ema50   = build_indicators(candles_1h)
    pending = []
    in_trade = False
    trade    = {}
    trades   = []

    for i, c in enumerate(candles_1h):
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")

        # ── Offene Position managen ───────────────────────────────────────────
        if in_trade:
            ae   = trade["ae"]
            sl   = trade["sl"]
            tp   = trade["tp"]
            risk = trade["risk"]
            d    = trade["direction"]

            if d == "short":
                hit_sl = c["high"] >= sl
                hit_tp = c["low"]  <= tp
            else:
                hit_sl = c["low"]  <= sl
                hit_tp = c["high"] >= tp

            if hit_sl and not hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                trade["pnl_r"]      = round(-1.0 - fee_r, 4)
                trade["exit_reason"] = "SL"
                trades.append(trade)
                in_trade = False
                continue

            if hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                trade["pnl_r"]      = round(tp_r - fee_r, 4)
                trade["exit_reason"] = "TP"
                trades.append(trade)
                in_trade = False
                continue
            continue

        # ── Pending Orders prüfen ─────────────────────────────────────────────
        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"]:
                    continue
                if day < start or day > end:
                    continue
                d    = p["direction"]
                stop = p["stop"]
                sl   = p["sl"]

                triggered_now = (d == "short" and c["low"]  <= stop) or \
                                (d == "long"  and c["high"] >= stop)

                if triggered_now:
                    if d == "short":
                        ae   = stop * (1 - SLIPPAGE)
                        risk = sl - ae
                        tp_price = ae - tp_r * risk
                    else:
                        ae   = stop * (1 + SLIPPAGE)
                        risk = ae - sl
                        tp_price = ae + tp_r * risk

                    if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.20:
                        triggered.append(p)
                        continue

                    in_trade = True
                    trade = {
                        "asset":     asset,
                        "direction": d,
                        "ae":        ae,
                        "sl":        sl,
                        "tp":        tp_price,
                        "risk":      risk,
                        "entry_day": day,
                    }
                    triggered.append(p)
                    break

            pending = [p for p in pending
                       if p not in triggered and i <= p["expiry"]]

        if in_trade:
            continue

        # ── Neues Signal suchen ───────────────────────────────────────────────
        if day < start or day > end or i < WARMUP:
            continue

        sig = detect_signal(candles_1h, ema50, i)
        if sig is None:
            continue

        if direction != "both" and sig["direction"] != direction:
            continue

        pending.append({
            "direction": sig["direction"],
            "stop":      sig["stop"],
            "sl":        sig["sl"],
            "risk":      sig["risk"],
            "expiry":    i + entry_window,
        })

    return trades


# ─── Statistik ────────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "total_r": 0.0,
                "pf": 0.0, "max_dd": 0.0, "p": 1.0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean) ** 2 for r in r_list) / (n - 1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0

    def erfc(x):
        t_ = 1 / (1 + 0.3275911 * abs(x))
        p  = t_ * (0.254829592 + t_ * (-0.284496736 + t_ * (1.421413741 +
              t_ * (-1.453152027 + t_ * 1.061405429))))
        return p * math.exp(-x * x)

    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {
        "n": n, "avg_r": round(mean, 3), "wr": round(len(wins) / n, 3),
        "total_r": round(total, 2), "pf": round(gw / gl, 2) if gl > 0 else float("inf"),
        "max_dd": round(dd, 2), "p": round(p, 4),
    }


def ascii_dist(r_list: list[float], bins: int = 20) -> str:
    if not r_list:
        return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi:
        return ""
    width = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        idx = min(int((r - lo) / width), bins - 1)
        counts[idx] += 1
    max_c = max(counts) or 1
    lines = []
    for idx, cnt in enumerate(counts):
        bar_lo = lo + idx * width
        bar = "█" * int(cnt / max_c * 20)
        lines.append(f"  {bar_lo:>+6.2f}R │{bar}")
    return "\n".join(lines)


# ─── Haupt-Runner ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KDT Scout")
    parser.add_argument("--assets", default=None,
                        help="Komma-getrennte Asset-Liste (z.B. SOL,ETH,BTC)")
    parser.add_argument("--tp",     type=float, default=TP_R,
                        help=f"TP in R (Standard: {TP_R})")
    parser.add_argument("--entry-window", type=int, default=ENTRY_WINDOW,
                        help=f"Stop-Order gültig für N Kerzen (Standard: {ENTRY_WINDOW})")
    parser.add_argument("--direction", default="both",
                        choices=["both", "short", "long"],
                        help="Nur SHORT, nur LONG oder beide (Standard: both)")
    parser.add_argument("--from",   dest="start", default="2025-04-21",
                        help="Backtest-Start (YYYY-MM-DD)")
    parser.add_argument("--to",     dest="end",   default="2026-04-19",
                        help="Backtest-Ende  (YYYY-MM-DD)")
    parser.add_argument("--detail", action="store_true",
                        help="Einzelne Trades ausgeben")
    args = parser.parse_args()

    assets = args.assets.split(",") if args.assets else DEFAULT_ASSETS
    # Nur Assets mit vorhandenen Daten
    assets = [a for a in assets
              if os.path.exists(os.path.join(PROJECT_DIR, "data", "historical",
                                             f"{a}_15m.csv"))]

    print(f"\n{'═'*80}")
    print(f"  KDT Scout — Kinetic Deceleration Trap")
    print(f"  EMA({EMA_PERIOD}) Kontext | 3-Kerzen-Sequenz | TP={args.tp}R | "
          f"Entry-Window={args.entry_window}")
    print(f"  Zeitraum: {args.start} → {args.end}")
    print(f"{'═'*80}\n")

    all_trades  = []
    asset_stats = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            print(f"  {asset:<6}: keine Daten — skip")
            continue

        candles_1h = aggregate_1h(candles_15m)
        trades     = run_kdt(asset, candles_1h, args.start, args.end,
                             args.tp, args.entry_window, args.direction)

        r_list = [t["pnl_r"] for t in trades]
        k      = kpis(r_list)
        all_trades.extend(trades)

        direction_counts = {"short": 0, "long": 0}
        for t in trades:
            direction_counts[t["direction"]] += 1

        print(f"  {asset:<6}  n={k['n']:>3}  AvgR={k['avg_r']:>+.3f}  "
              f"WR={k['wr']*100:>4.1f}%  PF={k['pf']:>4.1f}  "
              f"DD={k['max_dd']:.1f}R  p={k['p']:.3f}  "
              f"[S:{direction_counts['short']} L:{direction_counts['long']}]")

        if args.detail:
            for t in trades:
                print(f"      {t['entry_day']}  {t['direction']:<5}  {t['pnl_r']:>+.3f}R  [{t['exit_reason']}]")

        asset_stats.append({"asset": asset, **k,
                            "shorts": direction_counts["short"],
                            "longs":  direction_counts["long"]})

    # ── Gesamt-Statistik ──────────────────────────────────────────────────────
    all_r = [t["pnl_r"] for t in all_trades]
    total = kpis(all_r)

    print(f"\n{'─'*80}")
    print(f"  GESAMT   n={total['n']:>4}  AvgR={total['avg_r']:>+.3f}  "
          f"WR={total['wr']*100:>4.1f}%  PF={total['pf']:>4.1f}  "
          f"DD={total['max_dd']:.1f}R  p={total['p']:.4f}  "
          f"TotalR={total['total_r']:>+.1f}R")

    # Richtungs-Breakdown
    shorts = [t["pnl_r"] for t in all_trades if t["direction"] == "short"]
    longs  = [t["pnl_r"] for t in all_trades if t["direction"] == "long"]
    ks     = kpis(shorts)
    kl     = kpis(longs)
    print(f"\n  SHORT  n={ks['n']:>3}  AvgR={ks['avg_r']:>+.3f}  "
          f"WR={ks['wr']*100:>4.1f}%  PF={ks['pf']:>4.1f}")
    print(f"  LONG   n={kl['n']:>3}  AvgR={kl['avg_r']:>+.3f}  "
          f"WR={kl['wr']*100:>4.1f}%  PF={kl['pf']:>4.1f}")

    # Top-Assets
    print(f"\n  TOP ASSETS (AvgR):")
    for s in sorted(asset_stats, key=lambda x: -x["avg_r"])[:5]:
        print(f"     {s['asset']:<6}  AvgR={s['avg_r']:>+.3f}  n={s['n']}  "
              f"[S:{s['shorts']} L:{s['longs']}]")

    # R-Verteilung
    if all_r:
        print(f"\n  R-VERTEILUNG:")
        print(ascii_dist(all_r))

    # Gate-Check
    print(f"\n{'═'*80}")
    print(f"  SCOUT GATE CHECK")
    g1 = total["avg_r"] > 0
    g2 = total["p"] < 0.05
    g3 = total["n"] >= 50
    g4 = total["pf"] > 1.3
    print(f"  {'✅' if g1 else '❌'} Avg R > 0     : {total['avg_r']:>+.3f}R")
    print(f"  {'✅' if g2 else '❌'} p < 0.05      : {total['p']:.4f}")
    print(f"  {'✅' if g3 else '❌'} n ≥ 50        : {total['n']}")
    print(f"  {'✅' if g4 else '❌'} PF > 1.3      : {total['pf']:.2f}")
    gates_passed = sum([g1, g2, g3, g4])
    print(f"\n  → {gates_passed}/4 Gates bestanden")
    if gates_passed == 4:
        print(f"  ✅ SCOUT BESTANDEN — weiter zu WFA")
    elif gates_passed >= 2:
        print(f"  🟡 TEILWEISE — Parameter-Variation prüfen")
    else:
        print(f"  ❌ SCOUT NICHT BESTANDEN")
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
