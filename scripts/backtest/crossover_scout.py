#!/usr/bin/env python3
"""
CROSSOVER Scout — Krypto-Erschöpfungs-Edge auf Gold Futures (GC=F / XAUUSD)
=============================================================================
Frage: Überträgt sich der Mean-Reversion-Edge von VAA und KDT 1:1 auf TradFi?

Kernlogik UNVERÄNDERT. Nur angepasst:
  - Daten: XAUUSD_1h.csv (yfinance GC=F, 2 Jahre, 1H)
  - Fees:  CME Taker ~0.03% (statt Bitget 0.06%) + Slippage 0.05%

KDT — Kinetic Deceleration Trap:
  SHORT: 3 grüne Kerzen mit schrumpfenden Körpern + Volumen, Close > EMA50
         → Sell-Stop am Low der letzten Kerze, SL = High der letzten Kerze, TP = 3R
  LONG:  Symmetrisch, 3 rote Kerzen, Close < EMA50

VAA — Volume Absorption Anomaly:
  SHORT: Volumen > 3× SMA50, Body < 0.5× Body_SMA50, Close > EMA20
         → Sell-Stop am Low, SL = High, TP = 2R und 3R
  LONG:  Symmetrisch, Close < EMA20

Usage:
  venv/bin/python3 scripts/backtest/crossover_scout.py
"""
import csv
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))

DATA_PATH = os.path.join(PROJECT_DIR, "data", "historical", "XAUUSD_1h.csv")

# CME-Kosten (günstiger als Bitget)
TAKER_FEE = 0.0003   # 0.03%
SLIPPAGE  = 0.0005   # 0.05%


# ─── Daten ────────────────────────────────────────────────────────────────────

def load_gold() -> list[dict]:
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


# ─── Indikatoren (pure Python, identisch zu Krypto-Scouts) ───────────────────

def _ema(values: list[float], period: int) -> list[float]:
    out = [0.0] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def _sma(values: list[float], idx: int, period: int) -> float:
    if idx < period:
        return 0.0
    return sum(values[idx - period:idx]) / period


# ─── KPIs ────────────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "pf": 0.0,
                "total_r": 0.0, "max_dd": 0.0, "p": 1.0}
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
        t_ = 1 / (1 + 0.3275911 * abs(x))
        p  = t_ * (0.254829592 + t_ * (-0.284496736 + t_ * (
              1.421413741 + t_ * (-1.453152027 + t_ * 1.061405429))))
        return p * math.exp(-x * x)

    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {
        "n": n, "avg_r": round(mean, 4), "wr": round(len(wins) / n, 3),
        "total_r": round(total, 2), "pf": round(gw / gl, 2) if gl > 0 else float("inf"),
        "max_dd": round(dd, 2), "p": round(p, 4),
    }


def ascii_dist(r_list: list[float], bins: int = 18) -> str:
    if not r_list:
        return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi:
        return f"  [{lo:+.2f}R]\n"
    w = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r - lo) / w), bins - 1)] += 1
    max_c = max(counts) or 1
    lines = [f"  {lo+i*w:+.2f}R │{'█'*int(c/max_c*24)} ({c})"
             for i, c in enumerate(counts)]
    return "\n".join(lines)


def row(label, k, extra=""):
    flag = "✅" if k["avg_r"] > 0 and k["p"] < 0.05 and k["n"] >= 30 else \
           "⚠️ " if k["avg_r"] > 0 else "❌"
    print(f"  {flag} {label:<28} n={k['n']:>4}  AvgR={k['avg_r']:>+.4f}R  "
          f"WR={k['wr']*100:.1f}%  PF={k['pf']:.2f}  "
          f"MaxDD={k['max_dd']:.1f}R  p={k['p']:.4f}  {extra}")


# ═══════════════════════════════════════════════════════════════════════════════
# KDT — Kinetic Deceleration Trap (UNVERÄNDERTE LOGIK)
# ═══════════════════════════════════════════════════════════════════════════════

KDT_EMA     = 50
KDT_TP_R    = 3.0
KDT_WINDOW  = 2    # Stop-Order gültig für N Folgekerzen
KDT_WARMUP  = KDT_EMA + 5


def run_kdt(candles: list[dict], direction: str = "both") -> list[dict]:
    closes = [c["close"] for c in candles]
    ema50  = _ema(closes, KDT_EMA)

    pending  = []
    in_trade = False
    trade    = {}
    trades   = []

    for i, c in enumerate(candles):
        day = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        # ── Offene Position managen ───────────────────────────────────────────
        if in_trade:
            ae   = trade["ae"]
            sl   = trade["sl"]
            tp   = trade["tp"]
            risk = trade["risk"]
            d    = trade["direction"]

            hit_sl = (d == "short" and c["high"] >= sl) or \
                     (d == "long"  and c["low"]  <= sl)
            hit_tp = (d == "short" and c["low"]  <= tp) or \
                     (d == "long"  and c["high"] >= tp)

            if hit_sl and not hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                trade.update({"pnl_r": round(-1.0 - fee_r, 4), "exit": "SL", "exit_day": day})
                trades.append(trade)
                in_trade = False
                continue
            if hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                trade.update({"pnl_r": round(KDT_TP_R - fee_r, 4), "exit": "TP", "exit_day": day})
                trades.append(trade)
                in_trade = False
                continue
            continue

        # ── Pending prüfen ────────────────────────────────────────────────────
        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"]:
                    continue
                d    = p["direction"]
                stop = p["stop"]
                sl   = p["sl"]

                if (d == "short" and c["low"] <= stop) or \
                   (d == "long"  and c["high"] >= stop):
                    ae   = stop * (1 - SLIPPAGE) if d == "short" else stop * (1 + SLIPPAGE)
                    risk = (sl - ae) if d == "short" else (ae - sl)
                    if risk <= 0 or risk / ae < 0.0005 or risk / ae > 0.15:
                        triggered.append(p)
                        continue
                    tp_price = ae - KDT_TP_R * risk if d == "short" else ae + KDT_TP_R * risk
                    in_trade = True
                    trade = {
                        "direction": d, "ae": ae, "sl": sl,
                        "tp": tp_price, "risk": risk, "entry_day": day,
                    }
                    triggered.append(p)
                    break
            pending = [p for p in pending if p not in triggered and i <= p["expiry"]]

        if in_trade or i < KDT_WARMUP:
            continue

        # ── Signal suchen (IDENTISCHE LOGIK wie kdt_scout.py) ─────────────────
        if i < 2:
            continue
        c0, c1, c2 = candles[i], candles[i - 1], candles[i - 2]
        e = ema50[i]
        if e <= 0:
            continue

        body0 = abs(c0["close"] - c0["open"])
        body1 = abs(c1["close"] - c1["open"])
        body2 = abs(c2["close"] - c2["open"])
        if body0 <= 0:
            continue
        vol0, vol1, vol2 = c0["volume"], c1["volume"], c2["volume"]
        if vol0 <= 0:
            continue

        # SHORT: 3 grüne + schrumpfende Körper/Vol + Close > EMA50
        if (direction in ("short", "both") and
                c0["close"] > c0["open"] and c1["close"] > c1["open"] and c2["close"] > c2["open"] and
                body0 < body1 < body2 and vol0 < vol1 < vol2 and c0["close"] > e):
            sl   = c0["high"]
            stop = c0["low"]
            risk = sl - stop
            if risk > 0 and 0.0005 <= risk / stop <= 0.15:
                pending.append({"direction": "short", "stop": stop, "sl": sl,
                                "risk": risk, "expiry": i + KDT_WINDOW})

        # LONG: 3 rote + schrumpfende Körper/Vol + Close < EMA50
        elif (direction in ("long", "both") and
                c0["close"] < c0["open"] and c1["close"] < c1["open"] and c2["close"] < c2["open"] and
                body0 < body1 < body2 and vol0 < vol1 < vol2 and c0["close"] < e):
            sl   = c0["low"]
            stop = c0["high"]
            risk = stop - sl
            if risk > 0 and 0.0005 <= risk / stop <= 0.15:
                pending.append({"direction": "long", "stop": stop, "sl": sl,
                                "risk": risk, "expiry": i + KDT_WINDOW})

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# VAA — Volume Absorption Anomaly (UNVERÄNDERTE LOGIK)
# ═══════════════════════════════════════════════════════════════════════════════

VAA_EMA       = 20
VAA_VOL_SMA   = 50
VAA_BODY_SMA  = 50
VAA_VOL_MULT  = 3.0
VAA_BODY_MULT = 0.5
VAA_WINDOW    = 3
VAA_WARMUP    = max(VAA_VOL_SMA, VAA_BODY_SMA, VAA_EMA) + 2


def run_vaa(candles: list[dict], tp_r: float) -> list[dict]:
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    bodies  = [abs(c["open"] - c["close"]) for c in candles]
    ema20   = _ema(closes, VAA_EMA)

    pending  = []
    in_trade = False
    trade    = {}
    trades   = []

    for i, c in enumerate(candles):
        day = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        # ── Offene Position managen ───────────────────────────────────────────
        if in_trade:
            ae   = trade["ae"]
            sl   = trade["sl"]
            tp   = trade["tp"]
            risk = trade["risk"]
            d    = trade["direction"]

            hit_sl = (d == "long"  and c["low"]  <= sl) or \
                     (d == "short" and c["high"] >= sl)
            hit_tp = (d == "long"  and c["high"] >= tp) or \
                     (d == "short" and c["low"]  <= tp)

            if hit_sl and not hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                trade.update({"pnl_r": round(-1.0 - fee_r, 4), "exit": "SL", "exit_day": day})
                trades.append(trade)
                in_trade = False
                continue
            if hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                trade.update({"pnl_r": round(tp_r - fee_r, 4), "exit": "TP", "exit_day": day})
                trades.append(trade)
                in_trade = False
                continue
            continue

        # ── Pending prüfen ────────────────────────────────────────────────────
        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"]:
                    continue
                d    = p["direction"]
                stop = p["stop"]
                sl   = p["sl"]

                if (d == "long"  and c["high"] >= stop) or \
                   (d == "short" and c["low"]  <= stop):
                    ae   = stop * (1 + SLIPPAGE) if d == "long" else stop * (1 - SLIPPAGE)
                    risk = (ae - sl) if d == "long" else (sl - ae)
                    if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.25:
                        triggered.append(p)
                        continue
                    tp_price = ae + tp_r * risk if d == "long" else ae - tp_r * risk
                    in_trade = True
                    trade = {
                        "direction": d, "ae": ae, "sl": sl,
                        "tp": tp_price, "risk": risk, "entry_day": day,
                        "vol_ratio": p["vol_ratio"], "body_ratio": p["body_ratio"],
                    }
                    triggered.append(p)
                    break
            pending = [p for p in pending if p not in triggered and i <= p["expiry"]]

        if in_trade or i < VAA_WARMUP:
            continue

        # ── Anomalie suchen (IDENTISCHE LOGIK wie vaa_scout.py) ───────────────
        vol_sma  = _sma(volumes, i, VAA_VOL_SMA)
        body_sma = _sma(bodies,  i, VAA_BODY_SMA)
        e20      = ema20[i]

        if vol_sma <= 0 or body_sma <= 0 or e20 <= 0:
            continue

        vol_ratio  = c["volume"] / vol_sma
        body_ratio = bodies[i]  / body_sma

        if not (vol_ratio > VAA_VOL_MULT and body_ratio < VAA_BODY_MULT):
            continue

        # LONG: Close < EMA20
        if c["close"] < e20:
            sl   = c["low"]
            risk = c["high"] - sl
            if risk > 0 and risk / c["high"] < 0.25:
                pending.append({"direction": "long", "stop": c["high"], "sl": sl,
                                "expiry": i + VAA_WINDOW,
                                "vol_ratio": round(vol_ratio, 2),
                                "body_ratio": round(body_ratio, 3)})

        # SHORT: Close > EMA20
        if c["close"] > e20:
            sl   = c["high"]
            risk = sl - c["low"]
            if risk > 0 and risk / c["low"] < 0.25:
                pending.append({"direction": "short", "stop": c["low"], "sl": sl,
                                "expiry": i + VAA_WINDOW,
                                "vol_ratio": round(vol_ratio, 2),
                                "body_ratio": round(body_ratio, 3)})

    return trades


# ─── Main ────────────────────────────────────────────────────────────────────

def gate_check(k: dict, label: str) -> None:
    g1 = k["avg_r"] > 0
    g2 = k["p"] < 0.05
    g3 = k["n"] >= 30
    g4 = k["pf"] > 1.3
    score = sum([g1, g2, g3, g4])
    icon  = "✅" if score == 4 else "⚠️ " if score >= 2 else "❌"
    print(f"  {icon} {label}: {score}/4 Gates  "
          f"[AvgR>0:{g1}  p<.05:{g2}  n≥30:{g3}  PF>1.3:{g4}]")


def main():
    if not os.path.exists(DATA_PATH):
        print(f"❌ Keine Daten: {DATA_PATH}")
        return

    candles = load_gold()
    start   = datetime.fromtimestamp(candles[0]["time"]  / 1000).date()
    end     = datetime.fromtimestamp(candles[-1]["time"] / 1000).date()

    # Volumen-Check: wie viele Kerzen haben Volumen > 0?
    vol_ok = sum(1 for c in candles if c["volume"] > 0)

    print("═" * 78)
    print("  CROSSOVER SCOUT — Krypto-Edge auf Gold Futures (GC=F / XAUUSD)")
    print(f"  Daten: {start} – {end}  |  {len(candles)} 1H-Kerzen")
    print(f"  Volumen verfügbar: {vol_ok}/{len(candles)} Kerzen ({vol_ok/len(candles)*100:.0f}%)")
    print(f"  Fees: Taker {TAKER_FEE*100:.2f}% + Slippage {SLIPPAGE*100:.2f}%")
    print("═" * 78)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. KDT auf Gold
    # ──────────────────────────────────────────────────────────────────────────
    print("\n  ┌─ KDT — Kinetic Deceleration Trap (TP=3R, EMA50, 3-Kerzen-Sequenz) ─┐")

    kdt_both  = run_kdt(candles, "both")
    kdt_short = [t for t in kdt_both if t["direction"] == "short"]
    kdt_long  = [t for t in kdt_both if t["direction"] == "long"]

    row("KDT BOTH",  kpis([t["pnl_r"] for t in kdt_both]))
    row("KDT SHORT", kpis([t["pnl_r"] for t in kdt_short]))
    row("KDT LONG",  kpis([t["pnl_r"] for t in kdt_long]))

    if kdt_both:
        sl_n  = sum(1 for t in kdt_both if t["exit"] == "SL")
        tp_n  = sum(1 for t in kdt_both if t["exit"] == "TP")
        print(f"\n  Exit-Split: SL={sl_n} ({sl_n/len(kdt_both)*100:.0f}%)  "
              f"TP={tp_n} ({tp_n/len(kdt_both)*100:.0f}%)")
        print(f"\n  R-Verteilung KDT BOTH:")
        print(ascii_dist([t["pnl_r"] for t in kdt_both]))

    # ──────────────────────────────────────────────────────────────────────────
    # 2. VAA auf Gold
    # ──────────────────────────────────────────────────────────────────────────
    print("\n\n  ┌─ VAA — Volume Absorption Anomaly (Vol>3×, Body<0.5×, EMA20) ──────┐")

    for tp_r in [2.0, 3.0]:
        vaa_all   = run_vaa(candles, tp_r)
        vaa_short = [t for t in vaa_all if t["direction"] == "short"]
        vaa_long  = [t for t in vaa_all if t["direction"] == "long"]

        k_all = kpis([t["pnl_r"] for t in vaa_all])
        print(f"\n  ── TP = {tp_r}R ──────────────────────────────────────────────────────")
        row(f"VAA BOTH  (TP={tp_r}R)", k_all)
        row(f"VAA SHORT (TP={tp_r}R)", kpis([t["pnl_r"] for t in vaa_short]))
        row(f"VAA LONG  (TP={tp_r}R)", kpis([t["pnl_r"] for t in vaa_long]))

        if vaa_all:
            sl_n = sum(1 for t in vaa_all if t["exit"] == "SL")
            tp_n = sum(1 for t in vaa_all if t["exit"] == "TP")
            avg_vol = sum(t["vol_ratio"] for t in vaa_all) / len(vaa_all)
            print(f"  Exit: SL={sl_n} ({sl_n/len(vaa_all)*100:.0f}%)  "
                  f"TP={tp_n} ({tp_n/len(vaa_all)*100:.0f}%)  "
                  f"Ø Vol-Ratio={avg_vol:.1f}×")

    if vaa_all:
        print(f"\n  R-Verteilung VAA BOTH (TP=2R):")
        vaa_2r = run_vaa(candles, 2.0)
        print(ascii_dist([t["pnl_r"] for t in vaa_2r]))

    # ──────────────────────────────────────────────────────────────────────────
    # Gate-Check
    # ──────────────────────────────────────────────────────────────────────────
    print("\n\n" + "═" * 78)
    print("  GATE-CHECK — Vergleich mit Krypto-Baseline")
    print("─" * 78)
    print(f"  {'Strategie':<32} {'Krypto-Baseline':>16}  {'Gold':>16}")
    print(f"  {'KDT SHORT-only (Krypto IS)':<32} {'n=17, +0.45R, p=0.25':>16}")
    print(f"  {'VAA BOTH (Krypto OOS)':<32} {'n=28, +1.47R, p<0.05':>16}")
    print()
    gate_check(kpis([t["pnl_r"] for t in kdt_short]),   "KDT SHORT Gold")
    gate_check(kpis([t["pnl_r"] for t in kdt_long]),    "KDT LONG  Gold")
    gate_check(kpis([t["pnl_r"] for t in kdt_both]),    "KDT BOTH  Gold")
    vaa2 = run_vaa(candles, 2.0)
    gate_check(kpis([t["pnl_r"] for t in vaa2]),        "VAA BOTH  Gold (TP=2R)")
    vaa3 = run_vaa(candles, 3.0)
    gate_check(kpis([t["pnl_r"] for t in vaa3]),        "VAA BOTH  Gold (TP=3R)")
    print("═" * 78)


if __name__ == "__main__":
    main()
