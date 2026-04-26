#!/usr/bin/env python3
"""
IBB Scout — Initial Balance Breakout auf Gold Futures (XAUUSD / GC=F)
======================================================================
Edge-Hypothese:
  Der COMEX Gold Pit-Open (08:20 NY) zieht echten institutionellen Order-Flow.
  Die erste Handelsstunde (08:00–09:00 NY approximiert auf 1h-Basis) kondensiert
  diese Auktion zu einer Range (Initial Balance). Ausbrüche daraus sind
  gerichtete institutionelle Entscheidungen — kein Krypto-24/7-Rauschen.

Logik:
  IB        = High/Low der ersten 1H-Kerze ab 08:00 NY-Zeit (≈ COMEX-Open)
  LONG      = nächste Kerze(n) schließen über IB-High
  SHORT     = nächste Kerze(n) schließen unter IB-Low
  SL        = IB-Low (Long) / IB-High (Short)
  Exit      = Time-Stop: letzte Kerze vor 13:30 NY (COMEX Close)
  Max 1 Trade/Tag

Daten:
  data/historical/XAUUSD_1h.csv  (2 Jahre, 1h, via yfinance GC=F)
  Timestamps in UTC, Timezone-Handling via zoneinfo

Usage:
  venv/bin/python3 scripts/backtest/ibb_scout.py
"""
import csv
import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

DATA_PATH = os.path.join(PROJECT_DIR, "data", "historical", "XAUUSD_1h.csv")

NY          = ZoneInfo("America/New_York")
IB_HOUR     = 8    # Stunde 08:00 NY = Approximation COMEX-Open (08:20)
ENTRY_FROM  = 9    # Entry-Window ab 09:00 NY
EXIT_HOUR   = 13   # Time-Stop: letzte Kerze vor 13:30 NY
EXIT_MIN    = 30

SMA_PERIOD  = 50   # Trend-Gate: 50-Tage-SMA des Daily Close

# Kosten: kein Bitget hier, aber CME-Futures-Taker ~0.03% + Slippage 0.05%
TAKER_FEE  = 0.0003
SLIPPAGE   = 0.0005
COST_R     = (TAKER_FEE + SLIPPAGE) * 2  # Roundtrip


# ─── Daten laden ─────────────────────────────────────────────────────────────

def load_data() -> list[dict]:
    rows = []
    with open(DATA_PATH, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "time":   int(r["time_ms"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": float(r["volume"]),
            })
    rows.sort(key=lambda x: x["time"])
    return rows


def ny_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(NY)


# ─── Gruppen nach Handelstag ──────────────────────────────────────────────────

def group_by_day(candles: list[dict]) -> dict:
    days = {}
    for c in candles:
        dt = ny_dt(c["time"])
        if dt.weekday() >= 5:
            continue
        key = dt.date()
        days.setdefault(key, []).append(c)
    return days


def build_daily_sma(candles: list[dict], period: int = SMA_PERIOD) -> dict:
    """Berechnet SMA(period) des Daily Close. Key = date, Value = SMA oder None."""
    days = group_by_day(candles)
    sorted_dates = sorted(days.keys())
    # Daily Close = Close der letzten 1h-Kerze des Tages
    daily_closes = []
    for d in sorted_dates:
        closes = [c["close"] for c in days[d]]
        daily_closes.append((d, closes[-1]))
    sma = {}
    for i, (d, _) in enumerate(daily_closes):
        if i < period:
            sma[d] = None
            continue
        window = [c for _, c in daily_closes[i - period:i]]
        sma[d] = sum(window) / period
    return sma


# ─── KPIs ────────────────────────────────────────────────────────────────────

def _p(t_abs, df):
    if df <= 0:
        return 1.0
    z = t_abs * (1 - 1 / (4 * df)) / math.sqrt(1 + t_abs**2 / (2 * df))
    return max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))))


def kpis(r_list):
    n = len(r_list)
    if n == 0:
        return dict(n=0, avg_r=0.0, wr=0.0, pf=0.0, avg_win=0.0, avg_loss=0.0, p=1.0)
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r <= 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if n > 1:
        var = sum((r - avg_r)**2 for r in r_list) / (n - 1)
        std = math.sqrt(var) if var > 0 else 1e-9
        t   = avg_r / (std / math.sqrt(n))
    else:
        t = 0.0
    p = _p(abs(t), n - 1)
    return dict(n=n, avg_r=round(avg_r, 4), wr=round(wr, 4),
                pf=round(pf, 3), avg_win=round(avg_win, 3),
                avg_loss=round(avg_loss, 3), p=round(p, 4))


def ascii_dist(r_list, bins=18):
    if not r_list:
        return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi:
        return f"    [{lo:.2f}R]\n"
    w = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r - lo) / w), bins - 1)] += 1
    max_c = max(counts) or 1
    lines = [f"    {lo+i*w:+.2f}R │{'█'*int(c/max_c*24)} ({c})"
             for i, c in enumerate(counts)]
    return "\n".join(lines) + "\n"


# ─── Haupt-Backtest ───────────────────────────────────────────────────────────

def run(candles: list[dict], direction: str, tp_r: float = None) -> tuple:
    """
    direction: 'long' | 'short' | 'both'
    tp_r:      fixer TP in R-Vielfachen (z.B. 1.0, 1.5). None = reiner Time-Stop.
               SL = andere IB-Seite. Notbremse: Time-Stop 13:30 wenn weder TP noch SL.

    Candle-by-Candle SL/TP-Check: High ≥ TP (Long) oder Low ≤ TP (Long SL) innerhalb Kerze.
    SL wird vor TP geprüft (konservativ, wie in allen APEX-Scouts).
    """
    days   = group_by_day(candles)
    trades = []
    sl_hits = tp_hits = timeout_hits = skips = 0

    for date, day_candles in sorted(days.items()):
        ib_candle = None
        for c in day_candles:
            if ny_dt(c["time"]).hour == IB_HOUR:
                ib_candle = c
                break
        if ib_candle is None:
            skips += 1
            continue

        ib_high  = ib_candle["high"]
        ib_low   = ib_candle["low"]
        ib_range = ib_high - ib_low
        if ib_range <= 0:
            skips += 1
            continue

        entry_done = False
        for c in day_candles:
            dt = ny_dt(c["time"])
            if dt.hour < ENTRY_FROM:
                continue
            if dt.hour >= 12:
                break
            if entry_done:
                break

            is_long  = c["close"] > ib_high and direction in ("long", "both")
            is_short = c["close"] < ib_low  and direction in ("short", "both")
            if not (is_long or is_short):
                continue

            side    = "long" if is_long else "short"
            entry   = c["close"]
            sl      = ib_low  if side == "long" else ib_high
            sl_dist = abs(entry - sl)
            if sl_dist < 0.01:
                continue
            sl_pct = sl_dist / entry

            # TP-Level in Preis
            if tp_r is not None:
                tp_price = (entry + sl_dist * tp_r) if side == "long" \
                           else (entry - sl_dist * tp_r)
            else:
                tp_price = None

            entry_done = True
            result_r   = None
            exit_reason = "timeout"

            # Bar-by-Bar durch Post-Entry-Kerzen
            for ex in day_candles:
                ex_dt = ny_dt(ex["time"])
                if ex_dt <= dt:
                    continue

                # Time-Stop: Kerze startet nach 13:30 → vorherige war letzte
                past_exit = ex_dt.hour > EXIT_HOUR or \
                            (ex_dt.hour == EXIT_HOUR and ex_dt.minute >= EXIT_MIN)
                if past_exit:
                    # Time-Stop auf Close der letzten Kerze vor 13:30
                    break

                if side == "long":
                    # SL-first (konservativ)
                    if ex["low"] <= sl:
                        result_r = -1.0 - COST_R / sl_pct
                        exit_reason = "sl"
                        sl_hits += 1
                        break
                    if tp_price is not None and ex["high"] >= tp_price:
                        result_r = tp_r - COST_R / sl_pct
                        exit_reason = "tp"
                        tp_hits += 1
                        break
                else:  # short
                    if ex["high"] >= sl:
                        result_r = -1.0 - COST_R / sl_pct
                        exit_reason = "sl"
                        sl_hits += 1
                        break
                    if tp_price is not None and ex["low"] <= tp_price:
                        result_r = tp_r - COST_R / sl_pct
                        exit_reason = "tp"
                        tp_hits += 1
                        break
                last_ex = ex  # merken für Time-Stop-Close

            if result_r is None:
                # Time-Stop: Close der letzten Kerze vor 13:30
                try:
                    exit_price = last_ex["close"]
                    pnl_pct = (exit_price - entry) / entry if side == "long" \
                              else (entry - exit_price) / entry
                    result_r = pnl_pct / sl_pct - COST_R / sl_pct
                    timeout_hits += 1
                except NameError:
                    continue  # keine Kerze nach Entry gefunden

            trades.append({
                "date":   str(date),
                "side":   side,
                "r":      round(result_r, 4),
                "exit":   exit_reason,
            })

    return trades, sl_hits, tp_hits, timeout_hits, skips


# ─── Monats-Breakdown ─────────────────────────────────────────────────────────

def monthly_breakdown(trades: list[dict]) -> None:
    months = {}
    for t in trades:
        key = t["date"][:7]  # YYYY-MM
        months.setdefault(key, []).append(t["r"])
    print(f"\n  {'Monat':<10} │  n   AvgR    WR%   Total")
    print(f"  {'─'*10}─┼──────────────────────────")
    for m, rs in sorted(months.items()):
        n    = len(rs)
        avg  = sum(rs) / n
        wr   = sum(1 for r in rs if r > 0) / n
        tot  = sum(rs)
        flag = "🔴" if avg < 0 else "🟢"
        print(f"  {m:<10} │ {n:>3}  {avg:+.3f}R  {wr*100:>4.0f}%  {tot:+.2f}R  {flag}")


# ─── Main ────────────────────────────────────────────────────────────────────

def row(label, k):
    print(f"  {label:<30} │  n={k['n']:>4}  AvgR={k['avg_r']:+.4f}R"
          f"  WR={k['wr']*100:.1f}%  PF={k['pf']:.2f}"
          f"  AvgWin={k['avg_win']:+.3f}R  AvgLoss={k['avg_loss']:+.3f}R  p={k['p']:.4f}")


def exit_stats(trades: list[dict]) -> dict:
    sl  = sum(1 for t in trades if t["exit"] == "sl")
    tp  = sum(1 for t in trades if t["exit"] == "tp")
    to  = sum(1 for t in trades if t["exit"] == "timeout")
    n   = len(trades)
    return dict(sl=sl, tp=tp, timeout=to,
                sl_pct=sl/n*100 if n else 0,
                tp_pct=tp/n*100 if n else 0,
                to_pct=to/n*100 if n else 0)


def main():
    if not os.path.exists(DATA_PATH):
        print(f"❌ Keine Daten: {DATA_PATH}")
        return

    candles = load_data()
    print("═" * 82)
    print("  IBB-MATRIX — Initial Balance Breakout · Gold Futures (GC=F / XAUUSD)")
    print("  IB = 08:00–09:00 NY  |  Entry ab 09:00 NY  |  SL = andere IB-Seite")
    print("  Notbremse = Time-Stop 13:30 NY wenn weder TP noch SL")
    print(f"  Daten: {datetime.fromtimestamp(candles[0]['time']/1000).date()} – "
          f"{datetime.fromtimestamp(candles[-1]['time']/1000).date()}")
    print(f"  Kosten: Taker {TAKER_FEE*100:.2f}% + Slippage {SLIPPAGE*100:.2f}% "
          f"= {COST_R:.4f}R roundtrip  |  SL-first Simulation")
    print("═" * 82)

    # Exit-Matrix: 3 Varianten × 3 Richtungen
    variants = [
        ("V0 Time-Stop (Baseline)", None),
        ("V1 TP = 1.0R",            1.0),
        ("V2 TP = 1.5R",            1.5),
    ]
    directions = [("LONG", "long"), ("SHORT", "short"), ("BOTH", "both")]

    results = {}
    for v_label, tp in variants:
        for d_label, d in directions:
            trades, sl_h, tp_h, to_h, skips = run(candles, d, tp_r=tp)
            key = (v_label, d_label)
            results[key] = {"trades": trades, "r": [t["r"] for t in trades],
                            "sl": sl_h, "tp": tp_h, "timeout": to_h}

    # Tabelle: eine Zeile pro Variante × Richtung
    print(f"\n  {'Variante':<26} {'Dir':<6} │ "
          f"{'n':>4}  {'AvgR':>8}  {'WR':>6}  {'PF':>5}  "
          f"{'SL%':>5}  {'TP%':>5}  {'TO%':>5}  {'p':>6}")
    print("  " + "─" * 80)

    for v_label, tp in variants:
        for d_label, d in directions:
            key = (v_label, d_label)
            r_list = results[key]["r"]
            k  = kpis(r_list)
            sl = results[key]["sl"]
            tp_h = results[key]["tp"]
            to = results[key]["timeout"]
            n  = k["n"] or 1
            print(f"  {v_label:<26} {d_label:<6} │ "
                  f"{k['n']:>4}  {k['avg_r']:>+8.4f}  {k['wr']*100:>5.1f}%  "
                  f"{k['pf']:>5.2f}  "
                  f"{sl/n*100:>4.0f}%  {tp_h/n*100:>4.0f}%  {to/n*100:>4.0f}%  "
                  f"{k['p']:>6.4f}")
        print("  " + "·" * 80)

    # R-Verteilungen für die interessantesten
    print("\n" + "─" * 82)
    for v_label, tp in variants:
        print(f"\n  R-Verteilung {v_label} — BOTH:")
        print(ascii_dist(results[(v_label, "BOTH")]["r"]))

    # Monats-Breakdown für bestes Kandidat
    # Suche: welche Variante × Richtung hat höchstes AvgR?
    best_key = max(results, key=lambda k: kpis(results[k]["r"])["avg_r"])
    best_k   = kpis(results[best_key]["r"])
    print("─" * 82)
    print(f"\n  Bestes Ergebnis: {best_key[0]} | {best_key[1]}")
    print(f"  MONATS-BREAKDOWN:")
    monthly_breakdown(results[best_key]["trades"])

    print("\n" + "═" * 82)
    print("  URTEIL")
    print("─" * 82)
    for v_label, tp in variants:
        for d_label, _ in directions:
            key  = (v_label, d_label)
            k    = kpis(results[key]["r"])
            if k["n"] == 0:
                continue
            be_wr = 1 / (1 + (tp or 0)) if tp else None
            if k["avg_r"] > 0 and k["p"] < 0.05 and k["n"] >= 50:
                verdict = f"✅ GO  (p={k['p']:.4f}, n={k['n']})"
            elif k["avg_r"] > 0 and k["p"] < 0.10:
                verdict = f"⚠️  Schwach sig. (p={k['p']:.4f})"
            elif k["avg_r"] > 0:
                verdict = f"⚠️  Positiv n.s. (p={k['p']:.4f})"
            else:
                verdict = f"❌ NO-GO"
            print(f"  {v_label:<26} {d_label:<6}  AvgR={k['avg_r']:+.4f}R  {verdict}")
        print()
    print("═" * 82)


if __name__ == "__main__":
    main()
