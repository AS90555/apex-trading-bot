#!/usr/bin/env python3
"""
Funding-Rate Mean-Reversion Scout.

Theorie:
  Funding Rate (alle 8h) = erzwungene Zahlung zwischen Longs und Shorts.
  Sehr positives Funding → Markt überhebelt Long → Shorts sammeln Funding
    UND erwarten Mean-Reversion (Crowd falsch positioniert).
  Sehr negatives Funding → Markt überhebelt Short → Longs sammeln Funding.

Setup:
  Funding > +THRESHOLD  → SHORT (longs zahlen, SHORT kassiert + Reversion-Edge)
  Funding < -THRESHOLD  → LONG  (shorts zahlen, LONG kassiert + Reversion-Edge)

Entry  : 15m-Candle-Close nach dem Funding-Zeitstempel (00:00 / 08:00 / 16:00 UTC)
SL     : ATR(14) × SL_ATR_MULT unter/über Entry
TP     : ATR(14) × TP_ATR_MULT (alternativ: Time-Stop 24h)
Zusatz : Funding wird pro Periode als R-Beitrag gutgeschrieben (echte Kosten/Ertrag)

Verwendung:
  python3 scripts/backtest/funding_scout.py           # Download + Backtest
  python3 scripts/backtest/funding_scout.py --no-dl   # Nur Backtest (Daten vorhanden)
  python3 scripts/backtest/funding_scout.py --thresh 0.0003 --sl-atr 1.5 --tp-atr 3.0
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE

try:
    import requests
except ImportError:
    print("pip install requests"); sys.exit(1)

DEFAULT_ASSETS  = ["BTC","ETH","SOL","AVAX","XRP","DOGE","ADA","LINK","SUI","AAVE"]
FUNDING_DIR     = os.path.join(PROJECT_DIR, "data", "funding")
FUNDING_THRESH  = 0.0002   # 0.02% = 2× normales Funding (normal ~0.01%)
SL_ATR_MULT     = 2.0
TP_ATR_MULT     = 4.0
TIME_STOP_C     = 96       # 24h auf 15m
ATR_PERIOD      = 14


# ─── Download ────────────────────────────────────────────────────────────────

def download_funding(assets: list[str], start_ms: int, end_ms: int) -> dict:
    """Lädt Funding-Rate-History von Bitget. Gibt {asset: [{rate, ts}]} zurück."""
    os.makedirs(FUNDING_DIR, exist_ok=True)
    result = {}
    for asset in assets:
        symbol   = f"{asset}USDT"
        out_path = os.path.join(FUNDING_DIR, f"{asset}_funding.json")
        all_rows = []
        cursor_end = end_ms

        while True:
            params = {
                "symbol":      symbol,
                "productType": "USDT-FUTURES",
                "pageSize":    100,
                "endTime":     str(cursor_end),
            }
            try:
                r = requests.get(
                    "https://api.bitget.com/api/v2/mix/market/history-fund-rate",
                    params=params, timeout=10
                )
                data = r.json()
            except Exception as e:
                print(f"   ⚠️  {asset}: {e}"); break

            rows = data.get("data", [])
            if not rows:
                break

            for row in rows:
                ts = int(row["fundingTime"])
                if ts < start_ms:
                    rows = []  # Signal zum Stopp
                    break
                all_rows.append({"ts": ts, "rate": float(row["fundingRate"])})

            if not rows:
                break

            cursor_end = int(rows[-1]["fundingTime"]) - 1
            time.sleep(0.15)

        all_rows.sort(key=lambda x: x["ts"])
        all_rows = [r for r in all_rows if start_ms <= r["ts"] <= end_ms]
        with open(out_path, "w") as f:
            json.dump(all_rows, f)
        print(f"   {asset:<5}: {len(all_rows)} Funding-Perioden gespeichert")
        result[asset] = all_rows

    return result


def load_funding(assets: list[str]) -> dict:
    result = {}
    for asset in assets:
        path = os.path.join(FUNDING_DIR, f"{asset}_funding.json")
        if not os.path.exists(path):
            print(f"   ⚠️  Keine Funding-Daten für {asset} — erst --download ausführen")
            result[asset] = []
            continue
        with open(path) as f:
            result[asset] = json.load(f)
    return result


# ─── ATR-Berechnung ──────────────────────────────────────────────────────────

def calc_atr(candles: list[dict], idx: int, period: int = 14) -> float:
    if idx < period + 1:
        return 0.0
    window = candles[idx - period: idx]
    trs = []
    for i in range(1, len(window)):
        h, l, pc = window[i]["high"], window[i]["low"], window[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


# ─── Backtest ────────────────────────────────────────────────────────────────

def fee_adj(r: float, risk: float, entry: float) -> float:
    return r - (2 * entry * TAKER_FEE) / risk


def run_scout(assets: list[str], funding_data: dict, start: str, end: str,
              threshold: float, sl_mult: float, tp_mult: float) -> list[dict]:
    trades = []

    for asset in assets:
        candles  = load_csv(asset, "15m")
        funding  = funding_data.get(asset, [])
        if not candles or not funding:
            continue

        # Candle-Lookup: ts → index
        c_by_ts = {c["time"]: i for i, c in enumerate(candles)}
        c_times  = sorted(c_by_ts.keys())

        n_asset = 0

        for fr in funding:
            fr_ts = fr["ts"]
            rate  = fr["rate"]
            fr_dt = datetime.fromtimestamp(fr_ts / 1000, tz=timezone.utc)
            day   = fr_dt.strftime("%Y-%m-%d")

            if day < start or day > end:
                continue
            if abs(rate) < threshold:
                continue

            direction = "short" if rate > 0 else "long"

            # Erste 15m-Candle NACH dem Funding-Zeitstempel
            entry_ts = next((t for t in c_times if t >= fr_ts), None)
            if entry_ts is None:
                continue
            entry_idx = c_by_ts[entry_ts]
            if entry_idx >= len(candles) - TIME_STOP_C:
                continue

            entry_c = candles[entry_idx]
            atr     = calc_atr(candles, entry_idx)
            if atr <= 0:
                continue

            entry = entry_c["close"]
            ae    = entry * (1 + SLIPPAGE) if direction == "long" \
                    else entry * (1 - SLIPPAGE)
            sl    = (ae - sl_mult * atr) if direction == "long" \
                    else (ae + sl_mult * atr)
            tp    = (ae + tp_mult * atr) if direction == "long" \
                    else (ae - tp_mult * atr)
            risk  = abs(ae - sl)
            if risk <= 0 or risk / ae < 0.001:
                continue

            fee_in = ae * TAKER_FEE

            # Funding-Beitrag: SHORT kassiert positives Funding, LONG negatives
            funding_r = abs(rate) * ae / risk  # in R-Einheiten

            # Simulation
            net_r     = None
            exit_rsn  = None
            exit_day  = day
            n_candles = 0

            for j in range(1, TIME_STOP_C + 1):
                ci = entry_idx + j
                if ci >= len(candles):
                    break
                c = candles[ci]
                n_candles = j

                if direction == "long":
                    if c["low"] <= sl:
                        gross_r = -1.0
                        fee_out = c["low"] * TAKER_FEE / risk
                        net_r = gross_r - (fee_in + fee_out) + funding_r
                        exit_rsn = "sl"; break
                    if c["high"] >= tp:
                        gross_r = tp_mult * sl_mult  # tp_dist / sl_dist
                        gross_r = (tp - ae) / risk
                        fee_out = tp * TAKER_FEE / risk
                        net_r = gross_r - (fee_in + fee_out) + funding_r
                        exit_rsn = "tp"; break
                else:
                    if c["high"] >= sl:
                        gross_r = -1.0
                        fee_out = c["high"] * TAKER_FEE / risk
                        net_r = gross_r - (fee_in + fee_out) + funding_r
                        exit_rsn = "sl"; break
                    if c["low"] <= tp:
                        gross_r = (ae - tp) / risk
                        fee_out = tp * TAKER_FEE / risk
                        net_r = gross_r - (fee_in + fee_out) + funding_r
                        exit_rsn = "tp"; break

            if net_r is None and n_candles > 0:
                # Time-Stop
                last_c  = candles[entry_idx + n_candles]
                lp      = last_c["close"]
                gross_r = (lp - ae) / risk if direction == "long" else (ae - lp) / risk
                fee_out = lp * TAKER_FEE / risk
                net_r   = gross_r - (fee_in + fee_out) + funding_r
                exit_rsn = "timeout"
                exit_day = datetime.fromtimestamp(
                    last_c["time"]/1000, tz=timezone.utc).strftime("%Y-%m-%d")

            if net_r is None:
                continue

            trades.append({
                "asset":      asset,
                "direction":  direction,
                "day":        day,
                "funding_r":  round(rate * 10000, 2),  # in Basispunkten
                "net_r":      round(net_r, 4),
                "funding_contrib": round(funding_r, 4),
                "exit_reason": exit_rsn,
                "exit_day":   exit_day,
                "n_candles":  n_candles,
            })
            n_asset += 1

        print(f"   {asset:<5}: {n_asset} Trades (threshold={threshold*100:.3f}%)")

    return trades


# ─── Auswertung ──────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0: return {"n": 0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins); gl = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r - mean)**2 for r in r_list) / (n - 1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1.0/(1.0+0.3275911*abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p * math.exp(-x*x)
    p = erfc(abs(t)/math.sqrt(2)) if t != 0 else 1.0
    return {"n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
            "pf": gw/gl if gl > 0 else float("inf"), "sharpe": mean/sd if sd > 0 else 0,
            "max_dd": dd, "t": t, "p": p, "best": max(r_list), "worst": min(r_list)}


def print_results(trades: list[dict], threshold: float):
    rs = [t["net_r"] for t in trades]
    k  = kpis(rs)
    if not k.get("n"):
        print("  Keine Trades."); return

    pf  = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
    sig = "✅" if k["avg_r"] > 0.05 and k["p"] < 0.05 else \
          "⚠️ " if k["avg_r"] > 0 else "❌"

    print(f"\n  ═══ Funding Mean-Reversion  threshold={threshold*100:.3f}%  (n={k['n']}) ═══")
    print(f"  Win-Rate        : {k['wr']*100:.1f}%")
    print(f"  Avg R (inkl. FR): {k['avg_r']:>+8.4f}R  {sig}")
    print(f"  Total R         : {k['total_r']:>+8.2f}R")
    print(f"  Profit Factor   : {pf}")
    print(f"  Sharpe          : {k['sharpe']:>+8.3f}")
    print(f"  Max DD          : {k['max_dd']:>8.2f}R")
    print(f"  Best / Worst    : {k['best']:>+7.2f}R / {k['worst']:>+7.2f}R")
    print(f"  t / p           : {k['t']:>+8.3f} / {k['p']:.4f}")

    avg_fc = sum(t["funding_contrib"] for t in trades) / len(trades)
    print(f"  Ø Funding-Beitr.: {avg_fc:>+8.4f}R pro Trade")

    reasons = {}
    for t in trades: reasons[t["exit_reason"]] = reasons.get(t["exit_reason"],0)+1
    print(f"  Exits           : " + "  ".join(
        f"{r}={cnt}({cnt/k['n']*100:.0f}%)" for r,cnt in sorted(reasons.items(),key=lambda x:-x[1])))

    # Richtungs-Split
    for dl, d in [("SHORT (pos. Funding)", "short"), ("LONG (neg. Funding)", "long")]:
        sub = [t["net_r"] for t in trades if t["direction"] == d]
        if not sub: continue
        ks = kpis(sub)
        print(f"  → {dl} (n={len(sub):>3}): AvgR={ks['avg_r']:>+7.4f}R  "
              f"WR={ks['wr']*100:.1f}%  p={ks['p']:.4f}")

    # Cross-Asset
    print(f"\n  Cross-Asset:")
    assets = sorted(set(t["asset"] for t in trades))
    pos = 0
    for asset in assets:
        sub = [t["net_r"] for t in trades if t["asset"] == asset]
        ks  = kpis(sub)
        icon = "✅" if ks["avg_r"] > 0 else "❌"
        if ks["avg_r"] > 0: pos += 1
        fr_avg = sum(t["funding_r"] for t in trades if t["asset"] == asset)/len(sub)
        print(f"    {icon} {asset:<5}: n={len(sub):>3}  AvgR={ks['avg_r']:>+7.4f}R  "
              f"WR={ks['wr']*100:.0f}%  AvgFR={fr_avg:>+6.2f}bp")
    print(f"  Positive Assets : {pos}/{len(assets)}")

    # Funding-Verteilung
    all_fr = [t["funding_r"] for t in trades]
    print(f"\n  Funding-Rate-Verteilung (Basispunkte):")
    buckets = {}
    for fr in all_fr:
        b = round(fr, 0)
        buckets[b] = buckets.get(b, 0) + 1
    for b in sorted(buckets):
        bar = "█" * min(buckets[b], 40)
        print(f"    {b:>+6.0f}bp: {bar} ({buckets[b]})")

    print(f"\n  ═══ Scout-Entscheidung ═══")
    if k["avg_r"] > 0.05 and k["p"] < 0.05:
        print(f"  ✅ SIGNAL — vollständiger Optimierungs-Zyklus empfohlen")
    elif k["avg_r"] > 0 and k["p"] < 0.10:
        print(f"  ⚠️  SCHWACHES SIGNAL — Threshold-Optimierung prüfen")
    elif k["avg_r"] > 0:
        print(f"  ⚠️  POSITIV aber p={k['p']:.3f} — zu wenig Daten oder Noise")
    else:
        print(f"  ❌ KEIN SIGNAL")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets",  default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",    dest="start", default="2025-04-21")
    parser.add_argument("--to",      dest="end",   default="2026-04-19")
    parser.add_argument("--thresh",  type=float, default=FUNDING_THRESH, help="Funding-Threshold (z.B. 0.0002)")
    parser.add_argument("--sl-atr",  type=float, default=SL_ATR_MULT)
    parser.add_argument("--tp-atr",  type=float, default=TP_ATR_MULT)
    parser.add_argument("--no-dl",   action="store_true", help="Kein Download, Daten bereits lokal")
    args = parser.parse_args()
    assets = [a.strip().upper() for a in args.assets.split(",")]

    print(f"💰 Funding Rate Mean-Reversion Scout")
    print(f"   Assets    : {', '.join(assets)}")
    print(f"   Periode   : {args.start} → {args.end}")
    print(f"   Threshold : |rate| > {args.thresh*100:.3f}%")
    print(f"   SL/TP     : {args.sl_atr}×ATR / {args.tp_atr}×ATR  |  Time-Stop: 24h")
    print()

    start_ms = int(datetime.strptime(args.start, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000)
    end_ms   = int(datetime.strptime(args.end,   "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000) + 86400000

    if not args.no_dl:
        print("📥 Lade Funding-Rate-History...")
        funding = download_funding(assets, start_ms, end_ms)
    else:
        print("📂 Lade lokale Funding-Daten...")
        funding = load_funding(assets)

    print(f"\n🔍 Backtest läuft...")
    trades = run_scout(assets, funding, args.start, args.end,
                       args.thresh, args.sl_atr, args.tp_atr)
    print(f"\n   Gesamt Trades: {len(trades)}")

    if trades:
        print_results(trades, args.thresh)


if __name__ == "__main__":
    main()
