#!/usr/bin/env python3
"""
Phase 2 — Exit-Optimierung.

Simuliert 8 Exit-Strategien auf allen 1.750 PDH/PDL-Breakout-Setups (12 Monate, 10 Assets).
Pro Strategie: Gesamt-Kennzahlen, Walk-Forward (5 Folds), Monte Carlo (10k), t-Test.
Bonferroni-Korrektur: α = 0.05 / n_strategies.

Exportiert: data/analysis/trade_exit_strategies.jsonl
Report:     data/backtest_reports/phase2_exits.md

Verwendung:
  python3 scripts/backtest/phase2_exit_optimization.py
  python3 scripts/backtest/phase2_exit_optimization.py --no-scan  (nur aus JSONL)
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import (
    load_csv, aggregate_4h, aggregate_daily, calc_ema,
    EMA_FAST, EMA_SLOW, SL_BUFFER, TAKER_FEE, SLIPPAGE,
)
from scripts.backtest.walk_forward   import run_wfa, filter_trades_by_date
from scripts.backtest.monte_carlo    import run_monte_carlo
from scripts.backtest.hypothesis_tester import HypothesisTester, deflated_sharpe_ratio

ANALYSIS_DIR    = os.path.join(PROJECT_DIR, "data", "analysis")
EXIT_JSONL      = os.path.join(ANALYSIS_DIR, "trade_exit_strategies.jsonl")
DEFAULT_ASSETS  = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
TIMEOUT_CANDLES = 48
CANDLE_MINUTES  = 15


# ─── Exit-Strategien ─────────────────────────────────────────────────────────

def _fee_adj(r: float, risk: float, actual_entry: float) -> float:
    """R minus Roundtrip-Fees (ca. 0.12% vom Notional)."""
    fee = 2 * actual_entry * TAKER_FEE
    return r - fee / risk


def _calc_20d_vol(candles_15m: list[dict], entry_ts: int) -> float:
    """20-Tage Realized Volatility (annualisiert) aus täglichen Returns vor entry_ts."""
    prior = [c for c in candles_15m if c["time"] < entry_ts]
    # Tägliche Closes sammeln (UTC-Tag, letzte 15m-Candle pro Tag)
    day_closes = {}
    for c in prior:
        dt = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day_closes[dt.strftime("%Y-%m-%d")] = c["close"]
    days = sorted(day_closes.keys())
    if len(days) < 22:
        return 0.0
    closes = [day_closes[d] for d in days[-21:]]
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(365)  # annualisiert


def simulate_all_strategies(direction: str, entry: float, sl_default: float,
                            future_candles: list[dict], atr_14: float,
                            vol_20d: float = 0.0) -> dict:
    """
    Gibt dict mit r-Wert pro Strategie zurück.
    Alle Strategien nutzen den gleichen entry/future — nur Exit-Regeln differieren.
    """
    risk = abs(entry - sl_default)
    if risk <= 0:
        return {}

    actual_entry = entry * (1 + SLIPPAGE) if direction == "long" else entry * (1 - SLIPPAGE)

    def sl_hit(c, sl):
        return (direction == "long"  and c["low"]  <= sl) or \
               (direction == "short" and c["high"] >= sl)

    def tp_hit(c, tp):
        return (direction == "long"  and c["high"] >= tp) or \
               (direction == "short" and c["low"]  <= tp)

    def r_at_price(p):
        if direction == "long":
            return (p - actual_entry) / risk
        return (actual_entry - p) / risk

    atr_price = atr_14 if atr_14 else 0

    def sl_price_at_r(r_target):
        if direction == "long":
            return actual_entry + r_target * risk
        return actual_entry - r_target * risk

    def tp_price_at_r(r_target):
        if direction == "long":
            return actual_entry + r_target * risk
        return actual_entry - r_target * risk

    def run_fixed(sl_r, tp_r, tag):
        sl_p = sl_price_at_r(sl_r)
        tp_p = tp_price_at_r(tp_r)
        r_out = None
        for c in future_candles:
            if sl_hit(c, sl_p): r_out = sl_r; break
            if tp_hit(c, tp_p): r_out = tp_r; break
        if r_out is None:
            r_out = r_at_price(future_candles[-1]["close"]) if future_candles else 0.0
        return round(_fee_adj(r_out, risk, actual_entry), 4)

    results = {}

    # ─── H-BASELINE: SL=-1R, TP=+2R ─────────────────────────────────────────
    results["baseline_2r"] = run_fixed(-1.0, +2.0, "baseline_2r")

    # ─── H-200: TP-Grid (SL immer -1R, nur TP variiert) ─────────────────────
    for tp_val, tag in [(0.3, "tp_grid_03r"), (0.5, "tp_grid_05r"),
                        (0.75, "tp_grid_075r"), (1.0, "tp_grid_1r"),
                        (1.5, "tp_grid_15r")]:
        results[tag] = run_fixed(-1.0, tp_val, tag)

    # ─── H-201: ATR-adaptiver TP (TP = k × ATR14 / risk) ────────────────────
    if atr_price > 0:
        for k, tag in [(1.0, "atr_tp_1x"), (1.5, "atr_tp_15x"), (2.0, "atr_tp_2x")]:
            tp_dist = k * atr_price  # Preis-Distanz
            tp_r    = tp_dist / risk  # in R-Einheiten
            tp_r    = max(0.1, min(tp_r, 5.0))  # Clamp
            results[tag] = run_fixed(-1.0, tp_r, tag)
    else:
        for tag in ["atr_tp_1x", "atr_tp_15x", "atr_tp_2x"]:
            results[tag] = results["baseline_2r"]

    # ─── H-202: Time-Stop (SL=-1R, TP=+2R, Exit nach N Candles) ─────────────
    for ts_candles, tag in [(16, "time_stop_4h"), (32, "time_stop_8h"),
                            (48, "time_stop_12h")]:
        sl_ts = sl_price_at_r(-1.0)
        tp_ts = tp_price_at_r(+2.0)
        r_out = None
        for i, c in enumerate(future_candles):
            if sl_hit(c, sl_ts): r_out = -1.0; break
            if tp_hit(c, tp_ts): r_out = +2.0; break
            if i + 1 >= ts_candles:
                r_out = r_at_price(c["close"]); break
        if r_out is None:
            r_out = r_at_price(future_candles[-1]["close"]) if future_candles else 0.0
        results[tag] = round(_fee_adj(r_out, risk, actual_entry), 4)

    # ─── H-203: Chandelier-Trail nach +0.5R (plan-konform), Trail=Peak-1.5ATR ─
    if atr_price > 0:
        sl_ch = sl_price_at_r(-1.0)
        activated = False
        peak_price = actual_entry
        trail_sl = sl_ch
        r_out = None
        for c in future_candles:
            if activated:
                if direction == "long":
                    if c["high"] > peak_price: peak_price = c["high"]
                    new_sl = peak_price - 1.5 * atr_price
                    if new_sl > trail_sl: trail_sl = new_sl
                else:
                    if c["low"] < peak_price: peak_price = c["low"]
                    new_sl = peak_price + 1.5 * atr_price
                    if new_sl < trail_sl: trail_sl = new_sl
            if sl_hit(c, trail_sl):
                r_out = r_at_price(trail_sl); break
            if not activated:
                fav = c["high"] if direction == "long" else c["low"]
                if r_at_price(fav) >= 0.5:  # Aktivierung nach +0.5R (plan-konform)
                    activated = True
                    peak_price = fav
                    new_trail = (peak_price - 1.5 * atr_price) if direction == "long" \
                                 else (peak_price + 1.5 * atr_price)
                    trail_sl = max(trail_sl, new_trail) if direction == "long" \
                               else min(trail_sl, new_trail)
        if r_out is None:
            r_out = r_at_price(future_candles[-1]["close"]) if future_candles else 0.0
        results["chandelier_05r_15atr"] = round(_fee_adj(r_out, risk, actual_entry), 4)
    else:
        results["chandelier_05r_15atr"] = results["baseline_2r"]

    # ─── H-204: Partial-TP 50%@+0.5R + 50% mit BE-SL (plan-konform) ─────────
    sl_p  = sl_price_at_r(-1.0)
    tp1_p = tp_price_at_r(+0.5)   # plan: 0.5R (nicht 0.25R)
    be_sl = actual_entry
    tp1_hit_local = False
    r_first = 0.0
    r_out_second = None
    for c in future_candles:
        if not tp1_hit_local:
            if sl_hit(c, sl_p):
                r_first = -0.5; r_out_second = -0.5; break
            if tp_hit(c, tp1_p):
                tp1_hit_local = True
                r_first = 0.5 * 0.5  # 50% Position × +0.5R
                continue
        else:
            if sl_hit(c, be_sl):
                r_out_second = 0.0; break
    if r_out_second is None:
        if tp1_hit_local:
            r_out_second = 0.5 * r_at_price(future_candles[-1]["close"])
        else:
            r_final = r_at_price(future_candles[-1]["close"]) if future_candles else 0.0
            r_first = 0.5 * r_final; r_out_second = 0.5 * r_final
    results["partial_tp_05r_be"] = round(
        _fee_adj(r_first + r_out_second, risk, actual_entry), 4)

    # ─── H-205: Volatility-Adaptive TP (TP = k × 20d-Vol × entry / risk) ────
    if vol_20d > 0:
        daily_move = entry * vol_20d / math.sqrt(365)  # Tagesbewegung in Preis
        for k, tag in [(1.0, "vol_tp_1x"), (1.5, "vol_tp_15x"), (2.0, "vol_tp_2x")]:
            tp_dist = k * daily_move
            tp_r = min(max(tp_dist / risk, 0.1), 5.0)
            results[tag] = run_fixed(-1.0, tp_r, tag)
    else:
        for tag in ["vol_tp_1x", "vol_tp_15x", "vol_tp_2x"]:
            results[tag] = results["baseline_2r"]

    return results


# ─── ATR-Berechnung (14 Candles auf 15m vor Entry) ───────────────────────────

def calc_atr_at(candles_15m: list[dict], entry_ts: int, period: int = 14) -> float:
    """True Range Average über `period` letzte 15m-Candles vor entry_ts."""
    prior = [c for c in candles_15m if c["time"] < entry_ts]
    if len(prior) < period + 1:
        return 0.0
    recent = prior[-period-1:]
    trs = []
    for i in range(1, len(recent)):
        h = recent[i]["high"]
        l = recent[i]["low"]
        pc = recent[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs)


# ─── Haupt-Scan mit Strategie-Simulation ─────────────────────────────────────

def scan_trades(assets: list[str], start_date: str, end_date: str,
                verbose: bool = True) -> list[dict]:
    trades = []
    for asset in assets:
        candles_15m = load_csv(asset, "15m")
        if not candles_15m:
            continue
        candles_4h = aggregate_4h(candles_15m)
        daily_ohlc = aggregate_daily(candles_15m)
        closes_4h  = [c["close"] for c in candles_4h]
        ema_f = calc_ema(closes_4h, EMA_FAST)
        ema_s = calc_ema(closes_4h, EMA_SLOW)
        ema_by_ts = {candles_4h[i]["time"]: (ema_f[i], ema_s[i])
                     for i in range(len(candles_4h))}

        sorted_days = sorted(daily_ohlc.keys())
        n_asset = 0
        for i, day in enumerate(sorted_days):
            if day < start_date or day > end_date: continue
            if i == 0: continue
            prev = sorted_days[i - 1]
            pdh, pdl = daily_ohlc[prev]["high"], daily_ohlc[prev]["low"]
            if pdh <= pdl: continue

            day_dt    = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_start = int(day_dt.timestamp() * 1000)
            day_end   = int((day_dt + timedelta(days=1)).timestamp() * 1000)
            day_candles = [c for c in candles_15m if day_start <= c["time"] < day_end]
            if not day_candles: continue

            traded = False
            for j, candle in enumerate(day_candles):
                if traded: break
                price = candle["close"]
                dt_c = datetime.fromtimestamp(candle["time"] / 1000, tz=timezone.utc)
                bucket_h = (dt_c.hour // 4) * 4
                bucket_ts = int(datetime(dt_c.year, dt_c.month, dt_c.day,
                                         bucket_h, tzinfo=timezone.utc).timestamp() * 1000)
                ef, es = ema_by_ts.get(bucket_ts, (None, None))
                if ef is None or es is None: continue
                trend = "bull" if ef > es else "bear"

                direction = None
                if price > pdh and trend == "bull":
                    direction = "long"
                elif price < pdl and trend == "bear":
                    direction = "short"
                if direction is None: continue

                sl = pdl * (1 - SL_BUFFER) if direction == "long" else pdh * (1 + SL_BUFFER)
                risk_pct = abs(price - sl) / price
                if risk_pct < 0.001 or risk_pct > 0.15: continue

                future = day_candles[j+1:j+1+TIMEOUT_CANDLES]
                atr    = calc_atr_at(candles_15m, candle["time"])
                vol20  = _calc_20d_vol(candles_15m, candle["time"])
                strategy_results = simulate_all_strategies(
                    direction, price, sl, future, atr, vol20)
                if not strategy_results: continue

                trades.append({
                    "asset":    asset,
                    "day":      day,
                    "entry_ts": candle["time"],
                    "entry_hour": dt_c.hour,
                    "direction": direction,
                    "trend":    trend,
                    "atr":      round(atr, 6),
                    **strategy_results,
                })
                traded = True
                n_asset += 1
        if verbose:
            print(f"   {asset:<5}: {n_asset} Trades")
    return trades


# ─── Aggregation pro Strategie ───────────────────────────────────────────────

STRATEGIES = [
    # Baseline
    "baseline_2r",
    # H-200: TP-Grid (SL=1R fix)
    "tp_grid_03r", "tp_grid_05r", "tp_grid_075r", "tp_grid_1r", "tp_grid_15r",
    # H-201: ATR-adaptiver TP
    "atr_tp_1x", "atr_tp_15x", "atr_tp_2x",
    # H-202: Time-Stop
    "time_stop_4h", "time_stop_8h", "time_stop_12h",
    # H-203: Chandelier-Trail ab +0.5R
    "chandelier_05r_15atr",
    # H-204: Partial-TP 50%@0.5R + BE
    "partial_tp_05r_be",
    # H-205: Vol-Adaptive TP
    "vol_tp_1x", "vol_tp_15x", "vol_tp_2x",
]


def kpi(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0: return {"n": 0}
    wins = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw = sum(r for r in r_list if r > 0)
    gl = abs(sum(r for r in r_list if r < 0))
    mean = total / n
    var = sum((r - mean) ** 2 for r in r_list) / (n - 1) if n > 1 else 0
    sd = math.sqrt(var)
    # Max DD
    peak, cum, dd = 0.0, 0.0, 0.0
    for r in r_list:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {
        "n": n, "wr": len(wins)/n, "avg_r": mean, "total_r": total,
        "pf": gw/gl if gl > 0 else float("inf"),
        "sharpe": mean/sd if sd > 0 else 0, "max_dd": dd,
    }


def per_asset_avg_r(trades: list[dict], strategy: str) -> dict:
    by = {}
    for t in trades:
        by.setdefault(t["asset"], []).append(t[strategy])
    return {a: sum(rs)/len(rs) for a, rs in by.items()}


def print_strategy_table(trades: list[dict]):
    print(f"\n  === Strategie-Vergleich (Gesamt n={len(trades)}) ===")
    print(f"  {'Strategy':<26} {'WR':>6} {'AvgR':>9} {'TotalR':>10} {'PF':>7} {'Sharpe':>7} {'MaxDD':>7}")
    print(f"  {'-'*26} {'-'*6} {'-'*9} {'-'*10} {'-'*7} {'-'*7} {'-'*7}")
    for s in STRATEGIES:
        rs = [t[s] for t in trades]
        k = kpi(rs)
        pf = f"{k['pf']:.2f}" if k['pf'] != float('inf') else "∞"
        print(f"  {s:<26} {k['wr']*100:>5.1f}% {k['avg_r']:>+8.4f}R "
              f"{k['total_r']:>+9.2f}R {pf:>7} {k['sharpe']:>+6.3f} {k['max_dd']:>6.2f}R")


def main():
    parser = argparse.ArgumentParser(description="Phase 2 — Exit-Optimierung")
    parser.add_argument("--assets",  default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",    dest="start", default="2025-04-21")
    parser.add_argument("--to",      dest="end",   default="2026-04-19")
    parser.add_argument("--no-scan", action="store_true",
                        help="Trades nicht neu scannen, sondern aus JSONL lesen")
    parser.add_argument("--mc-iters", type=int, default=2000)
    args = parser.parse_args()

    print("🎯 Phase 2 — Exit-Optimierung")
    assets = [a.strip().upper() for a in args.assets.split(",")]

    if args.no_scan and os.path.exists(EXIT_JSONL):
        print(f"   Lade {EXIT_JSONL}")
        trades = []
        with open(EXIT_JSONL) as f:
            for line in f:
                line = line.strip()
                if line: trades.append(json.loads(line))
        print(f"   Trades: {len(trades)}")
    else:
        print(f"   Scanne {len(assets)} Assets von {args.start} bis {args.end}")
        trades = scan_trades(assets, args.start, args.end)
        os.makedirs(ANALYSIS_DIR, exist_ok=True)
        with open(EXIT_JSONL, "w") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
        print(f"   Export: {EXIT_JSONL}")

    if not trades:
        print("   ⚠️  keine Trades"); return

    # 1. Strategie-Vergleich
    print_strategy_table(trades)

    # 2. Hypothesis-Tests (Bonferroni)
    print(f"\n  === Hypothesis-Tests (Bonferroni, α=0.05, n_tests={len(STRATEGIES)}) ===")
    tester = HypothesisTester(alpha=0.05)
    for s in STRATEGIES:
        rs = [t[s] for t in trades]
        tester.register(s, rs, expected_effect="mean_r > 0")
    tester.print_summary()

    # 3. DSR für Top-Kandidaten (Sharpe-basiert)
    print(f"\n  === Deflated Sharpe Ratio (n_trials={len(STRATEGIES)}) ===")
    print(f"  {'Strategy':<26} {'SR':>8} {'DSR-Thresh':>12} {'Passes':>7}")
    print(f"  {'-'*26} {'-'*8} {'-'*12} {'-'*7}")
    for s in STRATEGIES:
        rs = [t[s] for t in trades]
        dsr = deflated_sharpe_ratio(rs, n_trials=len(STRATEGIES))
        icon = "✅" if dsr.get("sr_passes") else "❌"
        print(f"  {s:<26} {dsr.get('sr', 0):>+8.4f} {dsr.get('dsr_threshold', 0):>+12.4f} {icon:>5}")

    # 4. Walk-Forward pro Strategie
    print(f"\n  === Walk-Forward pro Strategie ===")
    print(f"  {'Strategy':<26} {'MeanIS':>9} {'MeanOOS':>9} {'WFE':>7} {'Pos/Total':>10}")
    print(f"  {'-'*26} {'-'*9} {'-'*9} {'-'*7} {'-'*10}")
    wfa_results = {}
    for s in STRATEGIES:
        # Fake trade-list mit custom exit_pnl für resolve_r
        fake = [{"day": t["day"], "mfe_r": 0, "mae_r": 0,
                 "sl_hit": False, "tp_2r_hit": False,
                 "sl_time_min": None, "tp_2r_time_min": None,
                 "final_r": t[s]}  # resolve_r baseline_2r fallthrough
                for t in trades]
        wfa = run_wfa(fake, exit_mode="baseline_2r")
        wfa_results[s] = wfa
        wfe_str = f"{wfa['wfe']:.3f}" if wfa['wfe'] is not None else "n/a"
        print(f"  {s:<26} {wfa['mean_is_r']:>+8.4f}R {wfa['mean_oos_r']:>+8.4f}R "
              f"{wfe_str:>7} {wfa['positive_folds']:>4}/{wfa['total_folds']}")

    # 5. Cross-Asset-Check
    print(f"\n  === Cross-Asset-Check (≥7/10 positive Assets = Akzeptanz) ===")
    print(f"  {'Strategy':<26} {'Positive Assets':>17}  Details")
    print(f"  {'-'*26} {'-'*17}  {'-'*60}")
    asset_pass = {}
    for s in STRATEGIES:
        per = per_asset_avg_r(trades, s)
        pos = sum(1 for a, r in per.items() if r > 0)
        asset_pass[s] = pos
        details = ", ".join(f"{a}:{r:+.3f}" for a, r in sorted(per.items()))
        icon = "✅" if pos >= 7 else "⚠️" if pos >= 5 else "❌"
        print(f"  {icon} {s:<24} {pos:>14}/10  {details}")

    # 6. Monte Carlo für Top-2 nach Avg R
    ranked = sorted(STRATEGIES,
                    key=lambda s: sum(t[s] for t in trades) / len(trades),
                    reverse=True)
    print(f"\n  === Monte Carlo (Top 3 Strategien, {args.mc_iters:,} Iterationen) ===")
    for s in ranked[:3]:
        rs = [t[s] for t in trades]
        mc = run_monte_carlo(rs, iterations=args.mc_iters)
        rz = mc["realized"]
        print(f"\n  {s}")
        print(f"    Final R: real {rz['final_r']:+.2f}R | P5 {mc['final_r']['p5']:+.2f}R | "
              f"P50 {mc['final_r']['p50']:+.2f}R | P95 {mc['final_r']['p95']:+.2f}R")
        print(f"    Max DD:  real {rz['max_dd_r']:+.2f}R | P5 {mc['max_dd_r']['p5']:+.2f}R | "
              f"P50 {mc['max_dd_r']['p50']:+.2f}R | P95 {mc['max_dd_r']['p95']:+.2f}R")
        print(f"    Sharpe:  real {rz['sharpe']:+.3f} | P5 {mc['sharpe']['p5']:+.3f} | "
              f"P95 {mc['sharpe']['p95']:+.3f}")


if __name__ == "__main__":
    main()
