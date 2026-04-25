#!/usr/bin/env python3
"""
Volume Absorption Anomaly (VAA) Scout — 1H-Timeframe.

Hypothese: Wenn bei extremem Volumen kaum Kerzenbewegung entsteht,
kämpft eine unsichtbare Limit-Order-Mauer gegen den Trend.
Sobald der Preis das Anomalie-High/Low durchbricht, übernimmt die andere Seite.

LONG Setup:
  Kontext  : Close < EMA(20)  [kurzfristiger Abwärtstrend]
  Effort   : Volumen > 3.0 × Vol_SMA(50)
  Result   : Kerzenkörper < 0.5 × Body_SMA(50)  [Doji bei Riesenvolumen]
  Entry    : Buy-Stop am High der Anomalie-Kerze (gültig 3 Folgekerzen)
  SL       : Low der Anomalie-Kerze
  TP       : 2R und 3R (beide getestet)

SHORT Setup (symmetrisch):
  Kontext  : Close > EMA(20)
  Effort   : Volumen > 3.0 × Vol_SMA(50)
  Result   : Kerzenkörper < 0.5 × Body_SMA(50)
  Entry    : Sell-Stop am Low der Anomalie-Kerze (gültig 3 Folgekerzen)
  SL       : High der Anomalie-Kerze

Verwendung:
  python3 scripts/backtest/vaa_scout.py
  python3 scripts/backtest/vaa_scout.py --vol-mult 2.5 --body-mult 0.4
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

DEFAULT_ASSETS  = ["BTC","ETH","SOL","AVAX","XRP","DOGE","ADA","LINK","SUI","AAVE"]
VOL_SMA_PERIOD  = 50
BODY_SMA_PERIOD = 50
EMA_PERIOD      = 20
ENTRY_WINDOW    = 3    # Kerzen lang ist Buy/Sell-Stop gültig
TP_LEVELS       = [2.0, 3.0]


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _ema_series(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i-1] * (1 - k)
    return result


def _sma(values: list[float], idx: int, period: int) -> float:
    if idx < period:
        return 0.0
    return sum(values[idx - period:idx]) / period


def build_indicators(candles: list[dict]) -> list[dict]:
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    bodies  = [abs(c["open"] - c["close"]) for c in candles]

    ema20 = _ema_series(closes, EMA_PERIOD)

    result = []
    for i in range(len(candles)):
        vol_sma  = _sma(volumes, i, VOL_SMA_PERIOD)
        body_sma = _sma(bodies,  i, BODY_SMA_PERIOD)
        result.append({
            "ema20":    ema20[i],
            "vol_sma":  vol_sma,
            "body_sma": body_sma,
            "body":     bodies[i],
            "volume":   volumes[i],
        })
    return result


# ─── Backtest ─────────────────────────────────────────────────────────────────

def run_scout(assets: list[str], start: str, end: str,
              vol_mult: float, body_mult: float,
              tp_r: float) -> list[dict]:
    trades = []
    warmup = max(VOL_SMA_PERIOD, BODY_SMA_PERIOD, EMA_PERIOD) + 2

    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            print(f"   {asset:<5}: keine Daten")
            continue

        candles = aggregate_1h(candles_15m)
        inds    = build_indicators(candles)
        n_asset = 0

        # Pending Buy/Sell-Stops: {direction, stop_price, sl, tp, expiry_idx, meta}
        pending = []
        in_trade  = False
        trade     = {}

        for i, c in enumerate(candles):
            dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")

            # ── Offene Position managen ───────────────────────────────────────
            if in_trade:
                ae   = trade["actual_entry"]
                sl   = trade["sl"]
                tp   = trade["tp"]
                risk = trade["risk"]
                direction = trade["direction"]

                # SL zuerst
                hit_sl = (direction == "long"  and c["low"]  <= sl) or \
                         (direction == "short" and c["high"] >= sl)
                hit_tp = (direction == "long"  and c["high"] >= tp) or \
                         (direction == "short" and c["low"]  <= tp)

                if hit_sl and not hit_tp:
                    fee_r = (2 * ae * TAKER_FEE) / risk
                    net_r = -1.0 - fee_r
                    trade.update({"net_r": round(net_r, 4), "exit_reason": "sl",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; n_asset += 1; continue

                if hit_tp:
                    gross_r = tp_r
                    fee_r   = (2 * ae * TAKER_FEE) / risk
                    net_r   = gross_r - fee_r
                    trade.update({"net_r": round(net_r, 4), "exit_reason": "tp",
                                  "exit_day": day, "n_candles": i - trade["entry_idx"]})
                    trades.append(trade); in_trade = False; n_asset += 1; continue
                continue

            # ── Pending Stop-Orders prüfen ────────────────────────────────────
            if not in_trade and pending:
                triggered = []
                for p in pending:
                    if i > p["expiry_idx"]:
                        continue  # abgelaufen
                    if day < start or day > end:
                        continue
                    direction  = p["direction"]
                    stop_price = p["stop_price"]

                    # Long: High der Kerze > Stop → Entry
                    if direction == "long" and c["high"] >= stop_price:
                        ae   = stop_price * (1 + SLIPPAGE)
                        sl   = p["sl"]
                        risk = ae - sl
                        if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.25:
                            continue
                        tp = ae + tp_r * risk
                        fee_entry = ae * TAKER_FEE
                        in_trade = True
                        trade = {
                            "asset":       asset,
                            "direction":   "long",
                            "trigger_day": p["trigger_day"],
                            "entry_day":   day,
                            "entry_idx":   i,
                            "actual_entry": round(ae, 6),
                            "sl":          round(sl, 6),
                            "tp":          round(tp, 6),
                            "risk":        risk,
                            "fee_entry":   fee_entry,
                            "vol_ratio":   p["vol_ratio"],
                            "body_ratio":  p["body_ratio"],
                        }
                        triggered.append(p)
                        break

                    elif direction == "short" and c["low"] <= stop_price:
                        ae   = stop_price * (1 - SLIPPAGE)
                        sl   = p["sl"]
                        risk = sl - ae
                        if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.25:
                            continue
                        tp = ae - tp_r * risk
                        fee_entry = ae * TAKER_FEE
                        in_trade = True
                        trade = {
                            "asset":       asset,
                            "direction":   "short",
                            "trigger_day": p["trigger_day"],
                            "entry_day":   day,
                            "entry_idx":   i,
                            "actual_entry": round(ae, 6),
                            "sl":          round(sl, 6),
                            "tp":          round(tp, 6),
                            "risk":        risk,
                            "fee_entry":   fee_entry,
                            "vol_ratio":   p["vol_ratio"],
                            "body_ratio":  p["body_ratio"],
                        }
                        triggered.append(p)
                        break

                # Abgelaufene und getriggerte entfernen
                pending = [p for p in pending
                           if p not in triggered and i <= p["expiry_idx"]]

            if in_trade:
                continue

            # ── Neue Anomalie suchen ──────────────────────────────────────────
            if day < start or day > end or i < warmup:
                continue

            ind = inds[i]
            if ind["vol_sma"] <= 0 or ind["body_sma"] <= 0 or ind["ema20"] <= 0:
                continue

            vol_ratio  = c["volume"] / ind["vol_sma"]
            body_ratio = ind["body"] / ind["body_sma"]

            big_vol   = vol_ratio  > vol_mult
            small_body = body_ratio < body_mult

            if not (big_vol and small_body):
                continue

            # LONG: Close unter EMA20
            if c["close"] < ind["ema20"]:
                sl = c["low"]
                approx_risk = c["high"] - sl
                if approx_risk > 0 and approx_risk / c["high"] < 0.25:
                    pending.append({
                        "direction":   "long",
                        "stop_price":  c["high"],
                        "sl":          round(sl, 6),
                        "expiry_idx":  i + ENTRY_WINDOW,
                        "trigger_day": day,
                        "vol_ratio":   round(vol_ratio, 2),
                        "body_ratio":  round(body_ratio, 3),
                    })

            # SHORT: Close über EMA20
            if c["close"] > ind["ema20"]:
                sl = c["high"]
                approx_risk = sl - c["low"]
                if approx_risk > 0 and approx_risk / c["low"] < 0.25:
                    pending.append({
                        "direction":   "short",
                        "stop_price":  c["low"],
                        "sl":          round(sl, 6),
                        "expiry_idx":  i + ENTRY_WINDOW,
                        "trigger_day": day,
                        "vol_ratio":   round(vol_ratio, 2),
                        "body_ratio":  round(body_ratio, 3),
                    })

        if in_trade and "net_r" not in trade:
            in_trade = False

        print(f"   {asset:<5}: {n_asset} Trades")

    return trades


# ─── KPIs + Output ────────────────────────────────────────────────────────────

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


def print_results(trades: list[dict], tp_r: float, vol_mult: float, body_mult: float):
    rs = [t["net_r"] for t in trades]
    k  = kpis(rs)

    print(f"\n  ═══ VAA Scout  TP={tp_r}R / Vol>{vol_mult}x / Body<{body_mult}x  (n={k.get('n',0)}) ═══")
    if not k.get("n"):
        print("  Keine Trades."); return

    pf  = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
    sig = "✅" if k["avg_r"] > 0.05 and k["p"] < 0.05 else \
          "⚠️ " if k["avg_r"] > 0 else "❌"

    print(f"  Win-Rate    : {k['wr']*100:.1f}%  (erwartet bei TP={tp_r}R: >{100/(1+tp_r):.0f}%)")
    print(f"  Avg R       : {k['avg_r']:>+8.4f}R  {sig}")
    print(f"  Total R     : {k['total_r']:>+8.2f}R")
    print(f"  Profit Fakt.: {pf}")
    print(f"  Sharpe      : {k['sharpe']:>+8.3f}")
    print(f"  Max DD      : {k['max_dd']:>8.2f}R")
    print(f"  Best/Worst  : {k['best']:>+7.2f}R / {k['worst']:>+7.2f}R")
    print(f"  t / p       : {k['t']:>+8.3f} / {k['p']:.4f}")

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    print(f"  Exits       : " + "  ".join(
        f"{r}={cnt}({cnt/k['n']*100:.0f}%)" for r, cnt in sorted(reasons.items(), key=lambda x:-x[1])))

    avg_hold = sum(t["n_candles"] for t in trades) / len(trades)
    print(f"  Ø Haltedauer: {avg_hold:.1f}h")

    # Richtungs-Split
    for label, d in [("LONG","long"),("SHORT","short")]:
        sub = [t["net_r"] for t in trades if t["direction"] == d]
        if not sub: continue
        ks = kpis(sub)
        print(f"  → {label} (n={len(sub):>3}): AvgR={ks['avg_r']:>+7.4f}R  "
              f"WR={ks['wr']*100:.0f}%  Total={ks['total_r']:>+7.2f}R  p={ks['p']:.4f}")

    # Cross-Asset
    print(f"\n  Cross-Asset:")
    assets_list = sorted(set(t["asset"] for t in trades))
    pos = 0
    for asset in assets_list:
        sub = [t["net_r"] for t in trades if t["asset"] == asset]
        ks  = kpis(sub)
        icon = "✅" if ks["avg_r"] > 0 else "❌"
        if ks["avg_r"] > 0: pos += 1
        avg_vr = sum(t["vol_ratio"]  for t in trades if t["asset"] == asset) / len(sub)
        print(f"    {icon} {asset:<5}: n={len(sub):>3}  AvgR={ks['avg_r']:>+7.4f}R  "
              f"WR={ks['wr']*100:.0f}%  AvgVol={avg_vr:.1f}x")
    print(f"  Positive Assets: {pos}/{len(assets_list)}")

    # Top-5
    top5 = sorted(trades, key=lambda t: t["net_r"], reverse=True)[:5]
    print(f"\n  Top-5 Trades:")
    for t in top5:
        print(f"    {t['asset']:<5} {t['direction']:<5} {t['trigger_day']}  "
              f"Vol={t['vol_ratio']:.1f}x  Body={t['body_ratio']:.3f}x  "
              f"→ {t['net_r']:>+6.2f}R [{t['exit_reason']}]")

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
    parser.add_argument("--assets",    default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",      dest="start",     default="2025-04-21")
    parser.add_argument("--to",        dest="end",       default="2026-04-19")
    parser.add_argument("--vol-mult",  type=float,       default=3.0)
    parser.add_argument("--body-mult", type=float,       default=0.5)
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    print(f"🧲 Volume Absorption Anomaly (VAA) Scout")
    print(f"   Assets    : {', '.join(assets)}")
    print(f"   Periode   : {args.start} → {args.end}")
    print(f"   Trigger   : Vol > {args.vol_mult}×SMA({VOL_SMA_PERIOD})  +  Body < {args.body_mult}×Body_SMA({BODY_SMA_PERIOD})")
    print(f"   Entry     : Buy/Sell-Stop am Anomalie-High/Low (gültig {ENTRY_WINDOW} Kerzen)")
    print(f"   Timeframe : 1H")
    print()

    for tp_r in TP_LEVELS:
        print(f"\n{'─'*60}")
        print(f"  TP = {tp_r}R")
        print(f"{'─'*60}")
        print(f"   Asset-Breakdown:")
        trades = run_scout(assets, args.start, args.end,
                           args.vol_mult, args.body_mult, tp_r)
        print(f"\n   Gesamt Trades: {len(trades)}")
        if trades:
            print_results(trades, tp_r, args.vol_mult, args.body_mult)


if __name__ == "__main__":
    main()
