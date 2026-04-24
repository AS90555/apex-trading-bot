#!/usr/bin/env python3
"""
SSR Scout — Statistical Spread Reversion (Pairs Trading BTC/ETH)
==================================================================
Hypothese: Der ETH/BTC-Close-Ratio ist auf 1H mean-reverting.
Extreme Z-Score-Abweichungen (±2.5σ über SMA 100) werden systematisch
korrigiert, bevor ein 48h-Time-Stop greift.

Trade-Struktur (Dollar-Neutral, 2 simultane Legs):
  Z > +ZSCORE_ENTRY  → Short ETH + Long BTC  (ETH zu teuer)
  Z < -ZSCORE_ENTRY  → Long ETH + Short BTC  (ETH zu billig)
  TP: Z kreuzt 0.0 (Mittelwert)
  SL: Time-Stop nach TIME_STOP_BARS Kerzen

Performance-Metrik: Netto-Return in % beider Legs (dollar-neutral)
Kein klassisches R (kein Preis-SL definierbar).

Usage:
  venv/bin/python3 scripts/backtest/ssr_scout.py
  venv/bin/python3 scripts/backtest/ssr_scout.py --sma 100 --zscore 2.5 --stop 48
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv

TAKER_FEE  = 0.0006   # 0.06% Taker je Seite
SLIPPAGE   = 0.0005   # 0.05% Slippage je Seite
# Beide Legs: Entry + Exit = 4× Taker + 4× Slippage = 0.44% total
ROUND_TRIP_COST = 4 * (TAKER_FEE + SLIPPAGE)

# ─── Parameter ────────────────────────────────────────────────────────────────
SMA_PERIOD      = 100    # Rolling-Fenster für SMA + StdDev der Ratio
ZSCORE_ENTRY    = 2.5    # Entry-Threshold
ZSCORE_EXIT     = 0.0    # TP wenn Z diesen Wert kreuzt
TIME_STOP_BARS  = 48     # Max Haltedauer in 1H-Kerzen

WARMUP = SMA_PERIOD + 5

ASSET_A = "ETH"
ASSET_B = "BTC"


# ─── 1H-Aggregation ───────────────────────────────────────────────────────────

def aggregate_1h(candles_15m: list[dict]) -> list[dict]:
    if not candles_15m:
        return []
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


# ─── Daten-Sync ───────────────────────────────────────────────────────────────

def load_and_merge(asset_a: str, asset_b: str) -> list[tuple[dict, dict]]:
    """Lädt beide Assets auf 1H und macht einen Inner Join über Timestamp."""
    raw_a = load_csv(asset_a, "15m")
    raw_b = load_csv(asset_b, "15m")
    if not raw_a or not raw_b:
        return []
    a_1h = aggregate_1h(raw_a)
    b_1h = aggregate_1h(raw_b)
    a_map = {c["time"]: c for c in a_1h}
    b_map = {c["time"]: c for c in b_1h}
    common_ts = sorted(set(a_map) & set(b_map))
    return [(a_map[ts], b_map[ts]) for ts in common_ts]


# ─── Rolling-Serien ───────────────────────────────────────────────────────────

def _rolling_sma(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    buf: list[float] = []
    for i, v in enumerate(values):
        buf.append(v)
        if len(buf) > period:
            buf.pop(0)
        if len(buf) == period:
            out[i] = sum(buf) / period
    return out


def _rolling_std(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    buf: list[float] = []
    for i, v in enumerate(values):
        buf.append(v)
        if len(buf) > period:
            buf.pop(0)
        if len(buf) == period:
            mean = sum(buf) / period
            var  = sum((x - mean) ** 2 for x in buf) / period  # Population-StdDev
            out[i] = math.sqrt(var) if var > 0 else float("nan")
    return out


# ─── KPIs (Return-basiert, kein R) ───────────────────────────────────────────

def kpis(returns: list[float]) -> dict:
    """
    returns: Liste von Netto-Returns in % pro Trade (nach Fees).
    """
    n = len(returns)
    if n == 0:
        return {"n": 0, "avg_ret": 0, "wr": 0, "total_ret": 0,
                "max_dd": 0, "sharpe": 0, "t": 0, "p": 1.0}
    wins   = [r for r in returns if r > 0]
    avg_r  = sum(returns) / n
    wr     = len(wins) / n
    # Max Drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in returns:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    # Sharpe annualisiert (8760 1H-Bars/Jahr)
    if n > 1:
        var    = sum((r - avg_r) ** 2 for r in returns) / (n - 1)
        std    = math.sqrt(var) if var > 0 else 1e-9
        sharpe = (avg_r / std) * math.sqrt(8760)
    else:
        std, sharpe = 0, 0
    # t-Test (Normalapproximation für n > 30)
    t = (avg_r / (std / math.sqrt(n))) if std > 0 and n > 1 else 0
    p = _p_approx(abs(t), n - 1) if n > 1 else 1.0
    return {
        "n": n,
        "avg_ret": round(avg_r * 100, 4),    # in Prozent
        "wr": round(wr * 100, 1),
        "total_ret": round(sum(returns) * 100, 2),
        "max_dd": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "t": round(t, 3),
        "p": round(p, 4),
    }


def _p_approx(t_abs: float, df: int) -> float:
    """Zweiseitiger p-Wert via Normalapproximation (gut für df > 10)."""
    if df <= 0:
        return 1.0
    # Cornish-Fisher Näherung für t → Normal
    z = t_abs * (1 - 1 / (4 * df)) / math.sqrt(1 + t_abs**2 / (2 * df))
    p = 2 * (1 - _norm_cdf(z))
    return max(0.0, min(1.0, p))


def _norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def ascii_dist(returns: list[float], bins: int = 20, label: str = "%") -> str:
    if not returns:
        return ""
    lo, hi = min(returns), max(returns)
    if lo == hi:
        return f"  [{lo*100:.3f}%] alle gleich"
    width  = (hi - lo) / bins
    counts = [0] * bins
    for r in returns:
        idx = min(int((r - lo) / width), bins - 1)
        counts[idx] += 1
    max_c = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        bar_lo = (lo + i * width) * 100
        bar    = "█" * int(c / max_c * 30)
        lines.append(f"  {bar_lo:+7.3f}% │{bar} ({c})")
    return "\n".join(lines)


# ─── Backtest-Core ────────────────────────────────────────────────────────────

def run_ssr(pairs: list[tuple[dict, dict]],
            sma_period: int = SMA_PERIOD,
            zscore_entry: float = ZSCORE_ENTRY,
            time_stop: int = TIME_STOP_BARS) -> dict:
    """
    pairs: [(eth_candle, btc_candle), ...] — synchron nach Timestamp sortiert.
    Gibt {"returns": [float], "durations": [int], "entry_zscores": [float],
           "exit_reasons": {"tp": int, "time_stop": int}} zurück.
    """
    # Spread-Serie berechnen (ETH/BTC Ratio)
    ratios = [a["close"] / b["close"] for a, b in pairs]
    sma_s  = _rolling_sma(ratios, sma_period)
    std_s  = _rolling_std(ratios, sma_period)

    returns:      list[float] = []
    durations:    list[int]   = []
    entry_zs:     list[float] = []
    exit_reasons = {"tp": 0, "time_stop": 0}

    trade: dict | None = None   # aktiver Trade

    for i in range(WARMUP, len(pairs)):
        eth, btc = pairs[i]
        sma = sma_s[i]
        std = std_s[i]

        if math.isnan(sma) or math.isnan(std) or std < 1e-10:
            continue

        z = (ratios[i] - sma) / std

        # ── 1. Offenen Trade managen ──────────────────────────────────────────
        if trade is not None:
            elapsed = i - trade["entry_bar"]

            # TP-Check: Z kreuzt 0 (Z-Score durch Null)
            prev_z = trade["entry_z"]
            z_crossed_zero = (prev_z > 0 and z <= ZSCORE_EXIT) or \
                             (prev_z < 0 and z >= ZSCORE_EXIT)

            # Wir aktualisieren prev_z-Tracker mit aktuellem Z für nächste Bar
            # (wir schauen ob Z seit Entry die 0 passiert hat)
            # Genauer: prüfe ob Z die Seite gewechselt hat
            entry_side = 1 if trade["entry_z"] > 0 else -1
            z_crossed_zero = (entry_side * z) <= 0

            if z_crossed_zero or elapsed >= time_stop:
                # Exit auf Close dieser Kerze
                exit_eth_close = eth["close"]
                exit_btc_close = btc["close"]

                entry_eth = trade["entry_eth"]
                entry_btc = trade["entry_btc"]
                direction = trade["direction"]   # +1: Long ETH / Short BTC, -1: Short ETH / Long BTC

                # Netto-Return (Dollar-Neutral: beide Legs gleich gewichtet)
                if direction == -1:   # Short ETH + Long BTC
                    pnl_eth = (entry_eth - exit_eth_close) / entry_eth   # Short ETH
                    pnl_btc = (exit_btc_close - entry_btc) / entry_btc   # Long BTC
                else:                  # Long ETH + Short BTC
                    pnl_eth = (exit_eth_close - entry_eth) / entry_eth   # Long ETH
                    pnl_btc = (entry_btc - exit_btc_close) / entry_btc   # Short BTC

                net_pnl = (pnl_eth + pnl_btc) / 2 - ROUND_TRIP_COST

                reason = "tp" if z_crossed_zero else "time_stop"
                exit_reasons[reason] += 1

                returns.append(net_pnl)
                durations.append(elapsed)
                entry_zs.append(trade["entry_z"])
                trade = None

                if z_crossed_zero:
                    continue   # Trade geschlossen — kein neuer Entry auf dieser Bar

        # ── 2. Signal-Erkennung (kein Trade offen) ───────────────────────────
        if trade is None:
            if z >= zscore_entry:
                # ETH zu teuer → Short ETH + Long BTC
                trade = {
                    "direction": -1,
                    "entry_bar": i,
                    "entry_z":   z,
                    "entry_eth": eth["close"],
                    "entry_btc": btc["close"],
                }
            elif z <= -zscore_entry:
                # ETH zu billig → Long ETH + Short BTC
                trade = {
                    "direction": +1,
                    "entry_bar": i,
                    "entry_z":   z,
                    "entry_eth": eth["close"],
                    "entry_btc": btc["close"],
                }

    return {
        "returns":      returns,
        "durations":    durations,
        "entry_zscores": entry_zs,
        "exit_reasons": exit_reasons,
    }


# ─── Spread-Analyse ───────────────────────────────────────────────────────────

def spread_analysis(pairs: list[tuple[dict, dict]], sma_period: int = SMA_PERIOD):
    """Zeigt wie oft Z±2.5 überhaupt erreicht wird — Signal-Frequenz-Check."""
    ratios = [a["close"] / b["close"] for a, b in pairs]
    sma_s  = _rolling_sma(ratios, sma_period)
    std_s  = _rolling_std(ratios, sma_period)

    z_scores = []
    for i in range(len(ratios)):
        if not math.isnan(sma_s[i]) and not math.isnan(std_s[i]) and std_s[i] > 1e-10:
            z_scores.append((ratios[i] - sma_s[i]) / std_s[i])

    if not z_scores:
        return

    abs_z = [abs(z) for z in z_scores]
    pct_above = {
        1.0: sum(1 for z in abs_z if z >= 1.0) / len(abs_z) * 100,
        1.5: sum(1 for z in abs_z if z >= 1.5) / len(abs_z) * 100,
        2.0: sum(1 for z in abs_z if z >= 2.0) / len(abs_z) * 100,
        2.5: sum(1 for z in abs_z if z >= 2.5) / len(abs_z) * 100,
        3.0: sum(1 for z in abs_z if z >= 3.0) / len(abs_z) * 100,
    }
    print(f"\n  Spread-Analyse: ETH/BTC Ratio auf {len(z_scores)} 1H-Bars")
    print(f"  Z-Score-Extrema (% der Zeit über Schwelle):")
    for thresh, pct in pct_above.items():
        bar = "█" * int(pct / 2)
        print(f"    |Z| ≥ {thresh:.1f}: {pct:5.1f}%  {bar}")
    print(f"  Z-Score Min/Max: {min(z_scores):+.2f} / {max(z_scores):+.2f}")
    print(f"  Z > 0 (ETH relativ teuer): {sum(1 for z in z_scores if z > 0) / len(z_scores)*100:.1f}%")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(sma_period: int = SMA_PERIOD,
         zscore_entry: float = ZSCORE_ENTRY,
         time_stop: int = TIME_STOP_BARS):

    print(f"\n{'='*62}")
    print(f"  SSR Scout — Statistical Spread Reversion")
    print(f"  Pair: {ASSET_A}/{ASSET_B}  SMA={sma_period}  Z-Entry=±{zscore_entry}")
    print(f"  TP: Z→0  Time-Stop: {time_stop}h  Fees: {ROUND_TRIP_COST*100:.2f}% p.Trade")
    print(f"{'='*62}")

    pairs = load_and_merge(ASSET_A, ASSET_B)
    if not pairs:
        print("  FEHLER: Keine Daten geladen.")
        return

    print(f"\n  Daten: {len(pairs)} synchrone 1H-Kerzen ({ASSET_A}/{ASSET_B} Inner Join)")

    # Spread-Analyse zuerst
    spread_analysis(pairs, sma_period)

    # Backtest
    res = run_ssr(pairs, sma_period, zscore_entry, time_stop)
    rets = res["returns"]
    durs = res["durations"]
    ez   = res["entry_zscores"]
    reasons = res["exit_reasons"]

    print(f"\n{'='*62}")
    print(f"  ERGEBNIS — {len(rets)} abgeschlossene Trades")
    print(f"{'='*62}")

    if not rets:
        print("  Keine Trades ausgeführt — Z-Threshold zu hoch oder zu wenig Daten.")
        return

    k = kpis(rets)
    tp_rate  = reasons["tp"]  / len(rets) * 100
    ts_rate  = reasons["time_stop"] / len(rets) * 100
    avg_dur  = sum(durs) / len(durs) if durs else 0
    avg_ez   = sum(abs(z) for z in ez) / len(ez) if ez else 0

    print(f"\n  Performance:")
    print(f"    n           = {k['n']}")
    print(f"    Avg Return  = {k['avg_ret']:+.4f}%  (nach {ROUND_TRIP_COST*100:.2f}% Fees)")
    print(f"    Win-Rate    = {k['wr']:.1f}%")
    print(f"    Total Return= {k['total_ret']:+.2f}%")
    print(f"    Sharpe      = {k['sharpe']:.2f}  (annualisiert)")
    print(f"    Max DD      = {k['max_dd']:.2f}%")
    print(f"    t / p       = {k['t']:.3f} / {k['p']:.4f}  "
          + ("✅ p<0.05" if k['p'] < 0.05 else "❌ p≥0.05"))
    print(f"\n  Trade-Charakteristik:")
    print(f"    TP-Rate     = {tp_rate:.1f}%  (Z kreuzt 0)")
    print(f"    Time-Stop   = {ts_rate:.1f}%  (nach {time_stop}h)")
    print(f"    Avg Dauer   = {avg_dur:.1f}h")
    print(f"    Avg |Z| Entry = {avg_ez:.2f}σ")

    wins  = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    if wins:
        print(f"    Avg Win     = {sum(wins)/len(wins)*100:+.4f}%")
    if losses:
        print(f"    Avg Loss    = {sum(losses)/len(losses)*100:+.4f}%")

    print(f"\n  Return-Verteilung:")
    print(ascii_dist(rets))

    # Scout-Gate
    print(f"\n{'='*62}")
    print(f"  Scout-Gate (Pairs Trading):")
    gate_n  = k['n'] >= 30
    gate_r  = k['avg_ret'] > 0
    gate_wr = k['wr'] > 50
    gate_p  = k['p'] < 0.05
    for name, ok, val in [
        ("n ≥ 30",         gate_n,  f"n={k['n']}"),
        ("Avg Return > 0%", gate_r,  f"{k['avg_ret']:+.4f}%"),
        ("WR > 50%",        gate_wr, f"{k['wr']:.1f}%"),
        ("p < 0.05",        gate_p,  f"p={k['p']:.4f}"),
    ]:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name:<22} {val}")

    all_go = gate_n and gate_r and gate_wr and gate_p
    verdict = "✅ GO  → WFA starten" if all_go else "❌ NO-GO"
    print(f"\n  Gesamt-Urteil: {verdict}")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sma",     type=int,   default=SMA_PERIOD)
    parser.add_argument("--zscore",  type=float, default=ZSCORE_ENTRY)
    parser.add_argument("--stop",    type=int,   default=TIME_STOP_BARS)
    args = parser.parse_args()
    main(args.sma, args.zscore, args.stop)
