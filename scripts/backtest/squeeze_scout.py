#!/usr/bin/env python3
"""
Volatility Squeeze Scout — 1H + 4H Timeframe.

Setup:
  Squeeze   : Bollinger Bands (20, 2.0) komplett innerhalb Keltner Channels (20, ATR×1.5)
  Trigger   : Squeeze in letzten 3 Kerzen aktiv + Close > BB_upper (LONG)
                                                  + Close < BB_lower (SHORT)
  SL        : max(Ausbruchskerze-Low, Entry − 1.5×ATR)   [Long]
              min(Ausbruchskerze-High, Entry + 1.5×ATR)  [Short]
  Exit      : Trailing — Close unter EMA(9) [Long] / über EMA(9) [Short]
  Time-Stop : 120 Candles (5 Tage auf 1H, ~20 Tage auf 4H)

Verwendung:
  python3 scripts/backtest/squeeze_scout.py
  python3 scripts/backtest/squeeze_scout.py --tf 4h --assets BTC,ETH,SOL
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

DEFAULT_ASSETS = ["BTC","ETH","SOL","AVAX","XRP","DOGE","ADA","LINK","SUI","AAVE"]
BB_PERIOD      = 20
BB_STDDEV      = 2.0
KC_PERIOD      = 20
KC_ATR_MULT    = 1.5
EMA_TRAIL      = 9
SL_ATR_MULT    = 1.5
SQUEEZE_BARS   = 3     # wie viele Bars Squeeze aktiv sein muss
TIME_STOP      = 120   # Candles


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _sma(candles: list[dict], idx: int, period: int, key: str = "close") -> float:
    if idx < period:
        return 0.0
    return sum(c[key] for c in candles[idx - period:idx]) / period


def _ema(candles: list[dict], idx: int, period: int) -> float:
    """Rekursive EMA — berechnet ab idx=period-1."""
    if idx < period - 1:
        return 0.0
    k = 2 / (period + 1)
    # Seed = SMA der ersten `period` Werte
    seed = sum(c["close"] for c in candles[:period]) / period
    val = seed
    for i in range(period, idx + 1):
        val = candles[i]["close"] * k + val * (1 - k)
    return val


def _ema_series(candles: list[dict], period: int) -> list[float]:
    """Berechnet EMA-Serie für alle Candles auf einmal (effizient)."""
    result = [0.0] * len(candles)
    if len(candles) < period:
        return result
    k = 2 / (period + 1)
    seed = sum(c["close"] for c in candles[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(candles)):
        result[i] = candles[i]["close"] * k + result[i-1] * (1 - k)
    return result


def _atr_series(candles: list[dict], period: int) -> list[float]:
    """ATR-Serie (Wilder's Smoothing)."""
    result = [0.0] * len(candles)
    if len(candles) < period + 1:
        return result
    # Seed = einfacher Durchschnitt der ersten `period` TRs
    trs = []
    for i in range(1, period + 1):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    seed = sum(trs) / period
    result[period] = seed
    for i in range(period + 1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        result[i] = (result[i-1] * (period - 1) + tr) / period
    return result


def _stddev(candles: list[dict], idx: int, period: int) -> float:
    if idx < period:
        return 0.0
    vals = [c["close"] for c in candles[idx - period:idx]]
    mean = sum(vals) / period
    return math.sqrt(sum((v - mean)**2 for v in vals) / period)


def build_indicators(candles: list[dict]) -> list[dict]:
    """Berechnet BB, KC, EMA-Trail für alle Candles."""
    n = len(candles)
    ema_kc  = _ema_series(candles, KC_PERIOD)   # Keltner-Mitte
    atr     = _atr_series(candles, KC_PERIOD)
    ema_tr  = _ema_series(candles, EMA_TRAIL)   # Trail-EMA

    indicators = []
    for i in range(n):
        # Bollinger Bands
        bb_mid = _sma(candles, i, BB_PERIOD)
        bb_sd  = _stddev(candles, i, BB_PERIOD)
        bb_up  = bb_mid + BB_STDDEV * bb_sd
        bb_lo  = bb_mid - BB_STDDEV * bb_sd

        # Keltner Channels
        kc_mid = ema_kc[i]
        kc_up  = kc_mid + KC_ATR_MULT * atr[i]
        kc_lo  = kc_mid - KC_ATR_MULT * atr[i]

        # Squeeze: BB komplett innerhalb KC
        squeeze = (bb_up < kc_up and bb_lo > kc_lo
                   and bb_mid > 0 and kc_mid > 0 and atr[i] > 0)

        indicators.append({
            "bb_up": bb_up, "bb_lo": bb_lo, "bb_mid": bb_mid,
            "kc_up": kc_up, "kc_lo": kc_lo,
            "atr":   atr[i],
            "ema_tr": ema_tr[i],
            "squeeze": squeeze,
        })
    return indicators


# ─── Aggregation ──────────────────────────────────────────────────────────────

def aggregate_1h(candles_15m: list[dict]) -> list[dict]:
    from datetime import datetime, timezone
    buckets = {}
    for c in candles_15m:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        bucket_ts = int(datetime(dt.year, dt.month, dt.day, dt.hour,
                                  tzinfo=timezone.utc).timestamp() * 1000)
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {"time": bucket_ts, "open": c["open"],
                                   "high": c["high"], "low": c["low"],
                                   "close": c["close"], "volume": c["volume"]}
        else:
            b = buckets[bucket_ts]
            b["high"]   = max(b["high"], c["high"])
            b["low"]    = min(b["low"],  c["low"])
            b["close"]  = c["close"]
            b["volume"] += c["volume"]
    return sorted(buckets.values(), key=lambda x: x["time"])


# ─── KPIs ─────────────────────────────────────────────────────────────────────

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


# ─── Backtest ─────────────────────────────────────────────────────────────────

def run_scout(assets: list[str], start: str, end: str, tf: str) -> list[dict]:
    trades = []

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            print(f"   {asset:<5}: keine Daten")
            continue

        if tf == "1h":
            candles = aggregate_1h(candles_15m)
        else:
            candles = aggregate_4h(candles_15m)

        inds = build_indicators(candles)
        n_asset = 0
        in_trade = False
        trade = {}

        warmup = max(BB_PERIOD, KC_PERIOD, EMA_TRAIL) + SQUEEZE_BARS + 2

        for i, c in enumerate(candles):
            dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")

            # ── Offene Position managen ───────────────────────────────────────
            if in_trade:
                direction = trade["direction"]
                ae   = trade["actual_entry"]
                sl   = trade["sl"]
                risk = trade["risk"]
                ema9 = inds[i]["ema_tr"]

                # SL zuerst (konservativ)
                if direction == "long" and c["low"] <= sl:
                    r = -1.0 - (2 * ae * TAKER_FEE) / risk
                    trade.update({"net_r": round(r, 4), "exit_reason": "sl",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; n_asset += 1; continue

                if direction == "short" and c["high"] >= sl:
                    r = -1.0 - (2 * ae * TAKER_FEE) / risk
                    trade.update({"net_r": round(r, 4), "exit_reason": "sl",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; n_asset += 1; continue

                # Trailing-Exit: EMA-9 Crossover (Close)
                if ema9 > 0:
                    trail_exit = (direction == "long"  and c["close"] < ema9) or \
                                 (direction == "short" and c["close"] > ema9)
                    if trail_exit:
                        ep = c["close"]
                        actual_exit = ep * (1 - SLIPPAGE) if direction == "long" \
                                      else ep * (1 + SLIPPAGE)
                        if direction == "long":
                            gross_r = (actual_exit - ae) / risk
                        else:
                            gross_r = (ae - actual_exit) / risk
                        fee_r = (2 * ae * TAKER_FEE) / risk
                        net_r = gross_r - fee_r
                        trade.update({"net_r": round(net_r, 4), "exit_reason": "trail",
                                      "exit_day": day, "n_candles": i - trade["entry_idx"]})
                        trades.append(trade); in_trade = False; n_asset += 1; continue

                # Time-Stop
                if (i - trade["entry_idx"]) >= TIME_STOP:
                    ep = c["close"]
                    actual_exit = ep * (1 - SLIPPAGE) if direction == "long" \
                                  else ep * (1 + SLIPPAGE)
                    if direction == "long":
                        gross_r = (actual_exit - ae) / risk
                    else:
                        gross_r = (ae - actual_exit) / risk
                    fee_r = (2 * ae * TAKER_FEE) / risk
                    net_r = gross_r - fee_r
                    trade.update({"net_r": round(net_r, 4), "exit_reason": "timeout",
                                  "exit_day": day, "n_candles": TIME_STOP})
                    trades.append(trade); in_trade = False; n_asset += 1
                continue

            # ── Entry-Check ───────────────────────────────────────────────────
            if day < start or day > end or i < warmup:
                continue

            ind = inds[i]
            if ind["atr"] <= 0 or ind["bb_mid"] <= 0:
                continue

            # Squeeze in letzten SQUEEZE_BARS Kerzen aktiv?
            squeeze_ok = all(inds[j]["squeeze"] for j in range(i - SQUEEZE_BARS, i))
            if not squeeze_ok:
                continue

            # Trigger
            direction = None
            if c["close"] > ind["bb_up"] and not inds[i]["squeeze"]:
                direction = "long"
            elif c["close"] < ind["bb_lo"] and not inds[i]["squeeze"]:
                direction = "short"

            if direction is None:
                continue

            # Entry: Open der nächsten Kerze
            if i + 1 >= len(candles):
                continue

            next_c = candles[i + 1]
            atr    = ind["atr"]

            if direction == "long":
                ae  = next_c["open"] * (1 + SLIPPAGE)
                sl  = max(c["low"], ae - SL_ATR_MULT * atr)
                # sl darf nicht über entry sein
                if sl >= ae:
                    sl = ae - SL_ATR_MULT * atr
            else:
                ae  = next_c["open"] * (1 - SLIPPAGE)
                sl  = min(c["high"], ae + SL_ATR_MULT * atr)
                if sl <= ae:
                    sl = ae + SL_ATR_MULT * atr

            risk = abs(ae - sl)
            if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.25:
                continue

            in_trade = True
            trade = {
                "asset":       asset,
                "direction":   direction,
                "trigger_day": day,
                "entry_idx":   i + 1,
                "actual_entry": round(ae, 6),
                "sl":          round(sl, 6),
                "risk":        risk,
                "fee_entry":   ae * TAKER_FEE,
                "atr":         round(atr, 6),
                "bb_up":       round(ind["bb_up"], 6),
                "bb_lo":       round(ind["bb_lo"], 6),
            }

        if in_trade and "net_r" not in trade:
            in_trade = False  # offener Trade am Datenende verwerfen

        print(f"   {asset:<5}: {n_asset} Trades")

    return trades


# ─── Output ───────────────────────────────────────────────────────────────────

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


def print_results(trades: list[dict], tf: str):
    rs = [t["net_r"] for t in trades]
    k  = kpis(rs)

    print(f"\n  ═══ Volatility Squeeze [{tf.upper()}]  (n={k.get('n',0)}) ═══")
    if not k.get("n"):
        print("  Keine Trades."); return

    pf  = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
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
    print(f"  Exits       : " + "  ".join(
        f"{r}={cnt}({cnt/k['n']*100:.0f}%)" for r, cnt in sorted(reasons.items(), key=lambda x: -x[1])))

    avg_hold_h = sum(t["n_candles"] for t in trades) / len(trades)
    hold_unit  = "h" if tf == "1h" else "×4h"
    print(f"  Ø Haltedauer: {avg_hold_h:.1f} Candles ({avg_hold_h * (1 if tf=='1h' else 4):.0f}h)")

    # Richtungs-Split
    for label, d in [("LONG","long"),("SHORT","short")]:
        sub = [t["net_r"] for t in trades if t["direction"] == d]
        if not sub: continue
        ks = kpis(sub)
        print(f"  → {label} (n={len(sub):>3}): AvgR={ks['avg_r']:>+7.4f}R  "
              f"WR={ks['wr']*100:.0f}%  Total={ks['total_r']:>+7.2f}R  p={ks['p']:.4f}")

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
              f"Best={ks['best']:>+6.2f}R")
    print(f"  Positive Assets: {pos}/{len(assets)}")

    # Top-5
    top5 = sorted(trades, key=lambda t: t["net_r"], reverse=True)[:5]
    print(f"\n  Top-5 Trades:")
    for t in top5:
        print(f"    {t['asset']:<5} {t['direction']:<5} {t['trigger_day']}  "
              f"→ {t['net_r']:>+6.2f}R [{t['exit_reason']}]")

    # R-Verteilung
    print(f"\n  R-Verteilung:")
    print(ascii_dist(rs))

    # Entscheidung
    print(f"\n  ═══ Scout-Entscheidung ═══")
    if k["avg_r"] > 0.05 and k["p"] < 0.05:
        print(f"  ✅ SIGNAL — vollständiger Zyklus empfohlen")
    elif k["avg_r"] > 0 and k["p"] < 0.10:
        print(f"  ⚠️  SCHWACHES SIGNAL — Parameter-Grid testen")
    elif k["avg_r"] > 0:
        print(f"  ⚠️  POSITIV aber p={k['p']:.3f} — nicht signifikant")
    else:
        print(f"  ❌ KEIN SIGNAL")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",   dest="start", default="2025-04-21")
    parser.add_argument("--to",     dest="end",   default="2026-04-19")
    parser.add_argument("--tf",     default="both", choices=["1h","4h","both"])
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    tfs = ["1h","4h"] if args.tf == "both" else [args.tf]

    for tf in tfs:
        print(f"\n🔧 Volatility Squeeze Scout [{tf.upper()}]")
        print(f"   Assets  : {', '.join(assets)}")
        print(f"   Periode : {args.start} → {args.end}")
        print(f"   BB({BB_PERIOD},{BB_STDDEV}) / KC({KC_PERIOD},{KC_ATR_MULT}) / Trail EMA({EMA_TRAIL})")
        print(f"   Squeeze: {SQUEEZE_BARS} Bars aktiv  |  SL: {SL_ATR_MULT}×ATR  |  Time-Stop: {TIME_STOP} Candles")
        print()

        trades = run_scout(assets, args.start, args.end, tf)
        print(f"\n   Gesamt Trades: {len(trades)}")
        if trades:
            print_results(trades, tf)


if __name__ == "__main__":
    main()
