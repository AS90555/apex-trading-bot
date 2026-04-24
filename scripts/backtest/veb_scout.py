#!/usr/bin/env python3
"""
VEB Scout — Volatility Expansion Breakout (TTM-Squeeze-Mechanik)
=================================================================
Hypothese: Phasen extremer Volatilitäts-Kompression (BB innerhalb KC für ≥5
Kerzen) bauen Druck auf. Ein Volumen-bestätigter Ausbruch aus der Kompression
leitet eine nachhaltige Trendbewegung ein.

Signal-Logik:
  Squeeze:  Upper_BB < Upper_KC UND Lower_BB > Lower_KC  (≥ SQUEEZE_MIN_BARS konsekutiv)
  LONG:     Close > Upper_BB  +  Vol > 1.5 × SMA(50)  →  nach ≥ SQUEEZE_MIN_BARS Squeeze
  SHORT:    Close < Lower_BB  +  Vol > 1.5 × SMA(50)  →  nach ≥ SQUEEZE_MIN_BARS Squeeze

Entry: Close der Ausbruchskerze (Market-Order)
SL:    SMA(20) der Ausbruchskerze  (BB-Mittellinie — Momentum ist tot wenn Preis zurück)
TP:    3.0R

Indikatoren (pure Python, O(n)):
  BB:  SMA(BB_PERIOD) ± BB_MULT × StdDev(BB_PERIOD)
  KC:  EMA(KC_PERIOD) ± KC_MULT × ATR(ATR_PERIOD)
  Vol: SMA(VOL_PERIOD)

Usage:
  venv/bin/python3 scripts/backtest/veb_scout.py
  venv/bin/python3 scripts/backtest/veb_scout.py --assets ETH,SOL --dir long
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

TAKER_FEE  = 0.0006
SLIPPAGE   = 0.0005

# ─── Parameter ────────────────────────────────────────────────────────────────
BB_PERIOD       = 20
BB_MULT         = 2.0
KC_PERIOD       = 20
KC_MULT         = 1.5
ATR_PERIOD      = 14
VOL_PERIOD      = 50
SQUEEZE_MIN_BARS = 5
VOL_TRIGGER_MULT = 1.5
TP_R            = 3.0

ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]
WARMUP = max(BB_PERIOD, KC_PERIOD, ATR_PERIOD, VOL_PERIOD) + SQUEEZE_MIN_BARS + 5


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


# ─── Indikator-Serien (O(n)) ──────────────────────────────────────────────────

def _sma_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    buf: list[float] = []
    for i, v in enumerate(values):
        buf.append(v)
        if len(buf) > period:
            buf.pop(0)
        if len(buf) == period:
            out[i] = sum(buf) / period
    return out


def _ema_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    k = 2 / (period + 1)
    ema = None
    for i, v in enumerate(values):
        if ema is None:
            ema = v
        else:
            ema = v * k + ema * (1 - k)
        if i >= period - 1:
            out[i] = ema
    return out


def _stddev_series(values: list[float], period: int) -> list[float]:
    out = [float("nan")] * len(values)
    buf: list[float] = []
    for i, v in enumerate(values):
        buf.append(v)
        if len(buf) > period:
            buf.pop(0)
        if len(buf) == period:
            mean = sum(buf) / period
            var  = sum((x - mean) ** 2 for x in buf) / period
            out[i] = math.sqrt(var)
    return out


def _atr_series(candles: list[dict], period: int = ATR_PERIOD) -> list[float]:
    """Wilder's ATR — O(n)."""
    out = [float("nan")] * len(candles)
    atr = None
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev_close = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"],
                     abs(c["high"] - prev_close),
                     abs(c["low"]  - prev_close))
        if atr is None:
            atr = tr
        else:
            atr = (atr * (period - 1) + tr) / period
        if i >= period - 1:
            out[i] = atr
    return out


# ─── KPIs ─────────────────────────────────────────────────────────────────────

def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0, "wr": 0, "pf": 0,
                "total_r": 0, "max_dd": 0, "sharpe": 0, "t": 0, "p": 1.0}
    wins   = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r < 0]
    avg_r  = sum(r_list) / n
    wr     = len(wins) / n
    pf     = sum(wins) / -sum(losses) if losses else float("inf")
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_list:
        equity += r
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    if n > 1:
        var    = sum((r - avg_r) ** 2 for r in r_list) / (n - 1)
        std    = math.sqrt(var) if var > 0 else 1e-9
        sharpe = (avg_r / std) * math.sqrt(8760)
        t      = avg_r / (std / math.sqrt(n))
    else:
        std, sharpe, t = 0, 0, 0
    p = _p_approx(abs(t), n - 1) if n > 1 else 1.0
    return {
        "n": n, "avg_r": round(avg_r, 4), "wr": round(wr, 4),
        "pf": round(pf, 3), "total_r": round(sum(r_list), 2),
        "max_dd": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "t": round(t, 3), "p": round(p, 4),
    }


def _p_approx(t_abs: float, df: int) -> float:
    if df <= 0: return 1.0
    z = t_abs * (1 - 1 / (4 * df)) / math.sqrt(1 + t_abs**2 / (2 * df))
    return max(0.0, min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))))


def ascii_dist(r_list: list[float], bins: int = 20) -> str:
    if not r_list: return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi: return f"  [{lo:.2f}R] alle gleich"
    width  = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r - lo) / width), bins - 1)] += 1
    max_c = max(counts) or 1
    lines = []
    for i, c in enumerate(counts):
        bar = "█" * int(c / max_c * 30)
        lines.append(f"  {lo + i*width:+6.2f}R │{bar} ({c})")
    return "\n".join(lines)


# ─── Backtest-Core ────────────────────────────────────────────────────────────

def run_veb(candles: list[dict], direction: str = "both") -> dict:
    closes  = [c["close"] for c in candles]
    highs   = [c["high"]  for c in candles]
    lows    = [c["low"]   for c in candles]
    volumes = [c["volume"] for c in candles]

    # Indikatoren vorberechnen
    sma20    = _sma_series(closes, BB_PERIOD)
    std20    = _stddev_series(closes, BB_PERIOD)
    ema20    = _ema_series(closes, KC_PERIOD)
    atr14    = _atr_series(candles, ATR_PERIOD)
    vol_sma  = _sma_series(volumes, VOL_PERIOD)

    longs:  list[float] = []
    shorts: list[float] = []
    signals = 0
    skips   = 0
    squeeze_streak = 0   # konsekutive Squeeze-Kerzen

    trade: dict | None = None

    for i in range(WARMUP, len(candles)):
        c = candles[i]

        # Alle Indikatoren dieser Kerze
        sma  = sma20[i]
        std  = std20[i]
        ema  = ema20[i]
        atr  = atr14[i]
        vsma = vol_sma[i]

        if any(math.isnan(x) for x in [sma, std, ema, atr, vsma]):
            skips += 1
            squeeze_streak = 0
            continue

        bb_upper = sma + BB_MULT * std
        bb_lower = sma - BB_MULT * std
        kc_upper = ema + KC_MULT * atr
        kc_lower = ema - KC_MULT * atr

        # Squeeze-Zustand dieser Kerze
        in_squeeze = (bb_upper < kc_upper) and (bb_lower > kc_lower)
        if in_squeeze:
            squeeze_streak += 1
        else:
            squeeze_streak = 0

        # ── Offenen Trade managen ─────────────────────────────────────────────
        if trade is not None:
            sl = trade["sl"]
            tp = trade["tp"]
            if trade["side"] == "long":
                if c["low"] <= sl:
                    r_gross = -1.0
                elif c["high"] >= tp:
                    r_gross = TP_R
                else:
                    trade = None if False else trade
                    continue   # Trade läuft noch
                fees_r = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["risk"]
                longs.append(round(r_gross - fees_r if r_gross > 0 else r_gross - fees_r, 3))
                trade = None
            else:  # short
                if c["high"] >= sl:
                    r_gross = -1.0
                elif c["low"] <= tp:
                    r_gross = TP_R
                else:
                    continue
                fees_r = (TAKER_FEE + SLIPPAGE) * 2 * trade["entry"] / trade["risk"]
                shorts.append(round(r_gross - fees_r if r_gross > 0 else r_gross - fees_r, 3))
                trade = None
            continue

        # ── Signal-Erkennung ─────────────────────────────────────────────────
        # Squeeze muss VORHER aktiv gewesen sein (streak zählt die bisherigen Bars)
        # squeeze_streak ist bereits mit dieser Kerze aktualisiert — wir prüfen
        # ob die VORHERIGE Bar im Squeeze war: dafür brauchen wir prev_streak.
        # Einfacher: squeeze_streak > SQUEEZE_MIN_BARS bedeutet wir sind gerade
        # aus dem Squeeze rausgekommen ODER noch drin. Wir wollen den Ausbruch.
        # Korrektur: Den Breakout erkennen wir wenn squeeze_streak == 0 (aktuelle
        # Kerze nicht mehr im Squeeze) UND die vorherige Streak ≥ MIN_BARS.
        # Aber wir setzen streak = 0 wenn NOT in_squeeze — das passiert erst nach
        # dem Breakout. Also: prüfe in_squeeze == False nach vorherigem Streak.

        # Alternativer Ansatz: Prüfe vorherige Kerze war im Squeeze
        if i > 0:
            prev_sma  = sma20[i-1]
            prev_std  = std20[i-1]
            prev_ema  = ema20[i-1]
            prev_atr  = atr14[i-1]
            if not any(math.isnan(x) for x in [prev_sma, prev_std, prev_ema, prev_atr]):
                prev_bb_u = prev_sma + BB_MULT * prev_std
                prev_bb_l = prev_sma - BB_MULT * prev_std
                prev_kc_u = prev_ema + KC_MULT * prev_atr
                prev_kc_l = prev_ema - KC_MULT * prev_atr
                prev_squeeze = (prev_bb_u < prev_kc_u) and (prev_bb_l > prev_kc_l)
            else:
                prev_squeeze = False
        else:
            prev_squeeze = False

        # Squeeze-Streak der vorherigen Bar brauchen wir eigentlich nicht neu —
        # squeeze_streak wurde vor dem Nullsetzen inkrementiert. Wenn aktuelle Bar
        # NOT in_squeeze und vorherige War in_squeeze, ist das der Ausbruch.
        # Wir brauchen wie lang der letzte Squeeze-Streak war. Dafür tracken wir
        # zusätzlich prev_streak.

        # Neuansatz ohne extra Variable: nutze in_squeeze und prev_squeeze.
        # Der Ausbruch passiert wenn: aktuelle Bar bricht BB UND prev war in Squeeze.
        # Für die Mindest-Länge: wir müssen zählen. Wir nutzen squeeze_streak BEVOR
        # wir ihn für die aktuelle Bar aktualisiert haben.

        # Ich refaktoriere: squeeze_streak wird am Ende der Loop-Iteration gesetzt.
        # Dafür wurde der Streak oben bereits gesetzt. Wenn in_squeeze == False und
        # squeeze_streak == 0 (nach Nullsetzen), weiß ich dass diese Kerze bricht.
        # Aber ich habe den Streak bereits auf 0 gesetzt — der alte Wert ist weg.

        # Lösung: Speichere prev_squeeze_streak separat.
        # Da wir keinen separaten Counter führen, nutzen wir eine andere Logik:
        # Prüfe ob diese Kerze NICHT im Squeeze ist (Breakout) und prüfe
        # retrospektiv ob die letzten SQUEEZE_MIN_BARS Kerzen alle im Squeeze waren.

        # Retrospektive Überprüfung der letzten N Kerzen (sauberste Methode):
        enough_squeeze = False
        if not in_squeeze and i >= SQUEEZE_MIN_BARS:
            all_squeezed = True
            for j in range(i - SQUEEZE_MIN_BARS, i):
                s = sma20[j]; sd = std20[j]; e = ema20[j]; a = atr14[j]
                if any(math.isnan(x) for x in [s, sd, e, a]):
                    all_squeezed = False
                    break
                bu = s + BB_MULT * sd; bl = s - BB_MULT * sd
                ku = e + KC_MULT * a;  kl = e - KC_MULT * a
                if not ((bu < ku) and (bl > kl)):
                    all_squeezed = False
                    break
            enough_squeeze = all_squeezed

        if not enough_squeeze:
            continue

        # Volumen-Filter
        if c["volume"] <= VOL_TRIGGER_MULT * vsma:
            skips += 1
            continue

        body = abs(c["close"] - c["open"])
        if body < 1e-9:
            skips += 1
            continue

        # LONG-Signal
        if direction in ("long", "both") and c["close"] > bb_upper:
            entry = c["close"]
            sl_price = sma   # BB-Mittellinie
            risk = entry - sl_price
            if risk < 1e-6:
                skips += 1
                continue
            tp_price = entry + risk * TP_R
            signals += 1
            trade = {"side": "long", "entry": entry, "sl": sl_price,
                     "tp": tp_price, "risk": risk}

        # SHORT-Signal
        elif direction in ("short", "both") and c["close"] < bb_lower:
            entry = c["close"]
            sl_price = sma
            risk = sl_price - entry
            if risk < 1e-6:
                skips += 1
                continue
            tp_price = entry - risk * TP_R
            signals += 1
            trade = {"side": "short", "entry": entry, "sl": sl_price,
                     "tp": tp_price, "risk": risk}

    return {"long": longs, "short": shorts,
            "signals": signals, "skips": skips}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(assets: list[str] | None = None, direction: str = "both"):
    assets = assets or ASSETS

    all_long:  list[float] = []
    all_short: list[float] = []
    total_signals = 0

    print(f"\n{'='*64}")
    print(f"  VEB Scout — Volatility Expansion Breakout (TTM-Squeeze)")
    print(f"  BB({BB_PERIOD},{BB_MULT})  KC({KC_PERIOD},{KC_MULT},ATR{ATR_PERIOD})")
    print(f"  Squeeze≥{SQUEEZE_MIN_BARS} Bars  Vol>{VOL_TRIGGER_MULT}×SMA({VOL_PERIOD})")
    print(f"  SL=BB-Midline  TP={TP_R}R  Direction={direction}")
    print(f"{'='*64}")

    for asset in assets:
        raw = load_csv(asset, "15m")
        if not raw:
            print(f"  {asset}: keine Daten")
            continue
        candles = aggregate_1h(raw)
        if len(candles) < WARMUP + 20:
            print(f"  {asset}: zu wenig Kerzen ({len(candles)})")
            continue

        res = run_veb(candles, direction)
        l, s = res["long"], res["short"]
        total_signals += res["signals"]

        kl = kpis(l) if l else None
        ks = kpis(s) if s else None

        print(f"\n  ── {asset} ({len(candles)} 1H-Kerzen, {res['signals']} Signale) ──")
        if l:
            print(f"    LONG   n={kl['n']:3d}  AvgR={kl['avg_r']:+.3f}  "
                  f"WR={kl['wr']*100:.0f}%  PF={kl['pf']:.2f}  "
                  f"TotalR={kl['total_r']:+.1f}  p={kl['p']:.4f}")
        else:
            print(f"    LONG   n=  0  — kein Signal")
        if s:
            print(f"    SHORT  n={ks['n']:3d}  AvgR={ks['avg_r']:+.3f}  "
                  f"WR={ks['wr']*100:.0f}%  PF={ks['pf']:.2f}  "
                  f"TotalR={ks['total_r']:+.1f}  p={ks['p']:.4f}")
        else:
            print(f"    SHORT  n=  0  — kein Signal")

        all_long.extend(l)
        all_short.extend(s)

    # Gesamt-Report
    print(f"\n{'='*64}")
    print(f"  GESAMT — {len(assets)} Assets  {total_signals} Signale")
    print(f"{'='*64}")

    for label, rs in [("LONG", all_long), ("SHORT", all_short),
                      ("BEIDE", all_long + all_short)]:
        if not rs:
            print(f"\n  {label}: keine Trades")
            continue
        k = kpis(rs)
        print(f"\n  {label}:")
        print(f"    n={k['n']}  AvgR={k['avg_r']:+.4f}  WR={k['wr']*100:.1f}%")
        print(f"    PF={k['pf']:.3f}  TotalR={k['total_r']:+.1f}R")
        print(f"    Sharpe={k['sharpe']:.2f}  MaxDD={k['max_dd']:.2f}R")
        print(f"    t={k['t']:.3f}  p={k['p']:.4f}  "
              + ("✅ p<0.05" if k['p'] < 0.05 else "❌ p≥0.05"))
        print(f"\n    R-Verteilung:")
        print(ascii_dist(rs))

    print(f"\n{'='*64}")
    print(f"  Scout-Gate: AvgR > 0 | p < 0.05 | n ≥ 30")
    for label, rs in [("LONG", all_long), ("SHORT", all_short)]:
        if not rs:
            print(f"  {label}: KEIN SIGNAL")
            continue
        k = kpis(rs)
        go = k['avg_r'] > 0 and k['p'] < 0.05 and k['n'] >= 30
        print(f"  {label}: AvgR={k['avg_r']:+.4f}  p={k['p']:.4f}  "
              f"n={k['n']}  → {'✅ GO → WFA' if go else '❌ NO-GO'}")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", default=",".join(ASSETS))
    parser.add_argument("--dir", choices=["long", "short", "both"], default="both")
    args = parser.parse_args()
    main(args.assets.split(","), direction=args.dir)
