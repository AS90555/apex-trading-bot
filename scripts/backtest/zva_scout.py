#!/usr/bin/env python3
"""
ZVA Scout — Zeit-Volumen-Asymmetrie, 15m-Chart, LONG-only.

Edge: Nach einem institutionellen Impuls (Body > BODY_ATR_MULT × ATR(56),
      Vol > VOL_MULT × Vol_SMA(200)) folgt ein schwacher, volumenarmer Rücklauf
      (≥ MIN_CANDLES Kerzen, Pullback-Avg-Vol < PULLBACK_VOL_MAX × SMA)
      → Limit-Entry bei 50% des Impulses, SL am Impuls-Low, TP = TP_R.

Parameter-Übersetzung 1H → 15m (Faktor 4):
  ATR_PERIOD    = 56  (14h × 4)
  VOL_SMA_PERIOD= 200 (50h × 4)
  MIN_CANDLES   = 20  (5h  × 4)
  MAX_CANDLES   = 48  (12h × 4)

State-Machine (pro Impuls):
  IDLE    → [Impuls erkannt]                       → PENDING
  PENDING → [50%-Level berührt UND Zeit+Vol OK]    → ENTRY → TRADE
  PENDING → [50%-Level berührt UND Bedingung FEHLT]→ EXPIRE → IDLE  ← Kern-Filter
  PENDING → [MAX_CANDLES abgelaufen ohne Berührung]→ EXPIRE → IDLE
  TRADE   → [SL-first Bar-by-Bar]                 → RESULT → IDLE

SHORT ist endgültig begraben: Krypto-Dumps sind Kaskaden oder V-Shapes,
keine sauberen Pullbacks — strukturelle Asymmetrie bestätigt im 1H-Test.

Verwendung:
  python3 scripts/backtest/zva_scout.py
  python3 scripts/backtest/zva_scout.py --assets ETH,BTC,SOL
  python3 scripts/backtest/zva_scout.py --body 1.2 --vol 1.8 --tp 2.0
  python3 scripts/backtest/zva_scout.py --detail --assets ETH
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

# ─── Konstanten (15m-kalibriert) ──────────────────────────────────────────────
BODY_ATR_MULT    = 1.5    # Impuls: Body > X × ATR(56)   [= 14h]
VOL_MULT         = 2.0    # Impuls: Vol  > X × SMA(200)  [= 50h]
MIN_CANDLES      = 20     # Zeit-Asymmetrie: ≥ 20 × 15m = 5h
MAX_CANDLES      = 48     # Gültigkeitsfenster: 48 × 15m = 12h
PULLBACK_VOL_MAX = 0.8    # Vol-Asymmetrie: Pullback-Avg < X × SMA
TP_R             = 3.0
ATR_PERIOD       = 56     # 14h in 15m-Kerzen
VOL_SMA_PERIOD   = 200    # 50h in 15m-Kerzen
WARMUP           = VOL_SMA_PERIOD + 5

IS_START = "2025-04-21"
IS_END   = "2026-02-10"

DEFAULT_ASSETS = [
    "ETH", "BTC", "SOL", "AVAX", "DOGE", "ADA",
    "SUI", "AAVE", "XRP", "LINK", "BNB", "INJ",
    "NEAR", "APT", "TIA", "OP", "ARB", "WIF",
]


# ─── Indikatoren (vorberechnet, O(n)) ────────────────────────────────────────

def _atr_series(candles: list, period: int = ATR_PERIOD) -> list:
    result = [0.0] * len(candles)
    if len(candles) < period + 1:
        return result
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    result[period] = atr
    for i in range(period + 1, len(candles)):
        atr = (atr * (period - 1) + trs[i - 1]) / period
        result[i] = atr
    return result


def _vol_sma_series(candles: list, period: int = VOL_SMA_PERIOD) -> list:
    result = [0.0] * len(candles)
    vols   = [c["volume"] for c in candles]
    for i in range(period - 1, len(candles)):
        result[i] = sum(vols[i - period + 1 : i + 1]) / period
    return result


# ─── Statistik ────────────────────────────────────────────────────────────────

def kpis(r_list: list) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "total_r": 0.0,
                "pf": 0.0, "max_dd": 0.0, "p": 1.0}
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
        p  = t_ * (0.254829592 + t_ * (-0.284496736 + t_ * (1.421413741 +
              t_ * (-1.453152027 + t_ * 1.061405429))))
        return p * math.exp(-x * x)

    p = erfc(abs(t) / math.sqrt(2)) if t != 0 else 1.0
    return {
        "n": n, "avg_r": round(mean, 3), "wr": round(len(wins) / n, 3),
        "total_r": round(total, 2), "pf": round(gw / gl, 2) if gl > 0 else float("inf"),
        "max_dd": round(dd, 2), "p": round(p, 4),
    }


def ascii_dist(r_list: list, bins: int = 20) -> str:
    if not r_list:
        return ""
    lo, hi = min(r_list), max(r_list)
    if lo == hi:
        return ""
    width  = (hi - lo) / bins
    counts = [0] * bins
    for r in r_list:
        counts[min(int((r - lo) / width), bins - 1)] += 1
    max_c = max(counts) or 1
    lines = []
    for idx, cnt in enumerate(counts):
        bar = "█" * int(cnt / max_c * 20)
        lines.append(f"  {lo + idx * width:>+6.2f}R │{bar}")
    return "\n".join(lines)


# ─── Haupt-Backtest ───────────────────────────────────────────────────────────

def run_zva(
    candles_1h: list,
    start: str,
    end:   str,
    direction:       str   = "both",
    body_atr_mult:   float = BODY_ATR_MULT,
    vol_mult:        float = VOL_MULT,
    min_candles:     int   = MIN_CANDLES,
    max_candles:     int   = MAX_CANDLES,
    pullback_vol_max:float = PULLBACK_VOL_MAX,
    tp_r:            float = TP_R,
    detail:          bool  = False,
) -> list:
    """
    Bar-by-Bar ZVA Backtest. Gibt Liste von R-Werten zurück.
    Jede Zeile ist ein abgeschlossener Trade (SL oder TP).
    """
    atr_s = _atr_series(candles_1h)
    sma_s = _vol_sma_series(candles_1h)

    start_ts = int(datetime.strptime(start, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ts   = int(datetime.strptime(end, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000) + 86_400_000

    results = []

    # ── Zustand ───────────────────────────────────────────────────────────────
    # pending: None | dict mit Impuls-Info
    # trade:   None | dict mit aktiver Position
    pending = None
    trade   = None

    for i in range(WARMUP, len(candles_1h)):
        c   = candles_1h[i]
        atr = atr_s[i]
        sma = sma_s[i]

        if atr <= 0 or sma <= 0:
            continue

        closed_this_bar = False

        # ── Schritt 1: Aktiven Trade SL-first überwachen ──────────────────────
        if trade is not None:
            d     = trade["direction"]
            entry = trade["entry"]
            sl    = trade["sl"]
            tp    = trade["tp"]
            risk  = abs(entry - sl)
            fees_r = (TAKER_FEE + SLIPPAGE) * 2 * entry / risk

            sl_hit = (d == "long"  and c["low"]  <= sl) or \
                     (d == "short" and c["high"] >= sl)
            tp_hit = (d == "long"  and c["high"] >= tp) or \
                     (d == "short" and c["low"]  <= tp)

            if sl_hit:     # SL-first
                r = -1.0 - fees_r
                results.append(r)
                if detail:
                    ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                    print(f"    SL  {ts.strftime('%Y-%m-%d %H:%M')}  "
                          f"{d.upper():<5}  entry={entry:.4f}  sl={sl:.4f}  r={r:+.3f}R")
                trade = None; pending = None; closed_this_bar = True
            elif tp_hit:
                r = tp_r - fees_r
                results.append(r)
                if detail:
                    ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                    print(f"    TP  {ts.strftime('%Y-%m-%d %H:%M')}  "
                          f"{d.upper():<5}  entry={entry:.4f}  tp={tp:.4f}  r={r:+.3f}R")
                trade = None; pending = None; closed_this_bar = True

        # ── Schritt 2: Pending-State-Machine ─────────────────────────────────
        if pending is not None and trade is None and not closed_this_bar:
            elapsed = i - pending["impulse_idx"]   # Kerzen seit Impuls

            # Jede Kerze nach dem Impuls zählt zum Rücklauf
            pending["pullback_vols"].append(c["volume"])
            avg_pb_vol = sum(pending["pullback_vols"]) / len(pending["pullback_vols"])
            vol_sma_imp = pending["vol_sma"]        # SMA zum Impuls-Zeitpunkt

            d      = pending["direction"]
            ep     = pending["entry"]               # 50%-Level = Limit-Order-Preis

            # Hat der Preis das Limit-Level BERÜHRT?
            touched = (d == "long"  and c["low"]  <= ep) or \
                      (d == "short" and c["high"] >= ep)

            if touched:
                # ── Kritischer Zweig: Bedingungen prüfen ─────────────────────
                time_ok = elapsed >= min_candles
                vol_ok  = avg_pb_vol < pullback_vol_max * vol_sma_imp

                if time_ok and vol_ok:
                    # ✅ ENTRY — Limit-Order ausgeführt
                    sl = pending["sl"]
                    if d == "long":
                        tp = ep + tp_r * (ep - sl)
                    else:
                        tp = ep - tp_r * (sl - ep)

                    trade = {
                        "direction":  d,
                        "entry":      ep,
                        "sl":         sl,
                        "tp":         tp,
                        "entry_time": c["time"],
                    }
                    if detail:
                        ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                        print(f"  → ENTRY {ts.strftime('%Y-%m-%d %H:%M')}  "
                              f"{d.upper():<5}  entry={ep:.4f}  sl={sl:.4f}  "
                              f"tp={tp:.4f}  elapsed={elapsed}  "
                              f"avg_vol={avg_pb_vol:.0f}  sma={vol_sma_imp:.0f}")
                    pending = None

                else:
                    # ❌ EXPIRE — Rücklauf zu schnell oder zu laut
                    reason = []
                    if not time_ok: reason.append(f"Zeit {elapsed}<{min_candles}")
                    if not vol_ok:  reason.append(f"Vol {avg_pb_vol/vol_sma_imp:.2f}x≥{pullback_vol_max}x")
                    if detail:
                        ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                        print(f"  × EXPIRE {ts.strftime('%Y-%m-%d %H:%M')}  "
                              f"{d.upper():<5}  [{' | '.join(reason)}]")
                    pending = None

            elif elapsed >= max_candles:
                # Timeout — Setup abgelaufen ohne Entry
                if detail:
                    ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                    print(f"  × TIMEOUT {ts.strftime('%Y-%m-%d %H:%M')}  "
                          f"elapsed={elapsed}>={max_candles}")
                pending = None

        # ── Schritt 3: Neuen Impuls suchen ───────────────────────────────────
        # Nur wenn IDLE, innerhalb des Datums-Fensters, und nicht gerade closed.
        if trade is None and not closed_this_bar:
            if start_ts <= c["time"] <= end_ts:

                body    = abs(c["close"] - c["open"])
                is_bull = c["close"] > c["open"]
                is_bear = c["close"] < c["open"]

                new_pending = None

                if direction in ("long", "both") and is_bull:
                    if body > body_atr_mult * atr and c["volume"] > vol_mult * sma:
                        mid = c["low"] + (c["high"] - c["low"]) * 0.5
                        new_pending = {
                            "direction":   "long",
                            "impulse_idx": i,
                            "entry":       mid,
                            "sl":          c["low"],
                            "vol_sma":     sma,
                            "pullback_vols": [],
                        }
                        if detail:
                            ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                            print(f"IMPULSE {ts.strftime('%Y-%m-%d %H:%M')}  "
                                  f"LONG  body={body:.2f}({body/atr:.1f}×ATR)  "
                                  f"vol={c['volume']:.0f}({c['volume']/sma:.1f}×SMA)  "
                                  f"entry@{mid:.4f}  sl@{c['low']:.4f}")

                if direction in ("short", "both") and is_bear and new_pending is None:
                    if body > body_atr_mult * atr and c["volume"] > vol_mult * sma:
                        mid = c["low"] + (c["high"] - c["low"]) * 0.5
                        new_pending = {
                            "direction":   "short",
                            "impulse_idx": i,
                            "entry":       mid,
                            "sl":          c["high"],
                            "vol_sma":     sma,
                            "pullback_vols": [],
                        }
                        if detail:
                            ts = datetime.fromtimestamp(c["time"]/1000, tz=timezone.utc)
                            print(f"IMPULSE {ts.strftime('%Y-%m-%d %H:%M')}  "
                                  f"SHORT body={body:.2f}({body/atr:.1f}×ATR)  "
                                  f"vol={c['volume']:.0f}({c['volume']/sma:.1f}×SMA)  "
                                  f"entry@{mid:.4f}  sl@{c['high']:.4f}")

                # Neuer Impuls überschreibt altes Pending (frischerer Setup)
                if new_pending is not None:
                    pending = new_pending

    return results


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ZVA Scout — Zeit-Volumen-Asymmetrie")
    parser.add_argument("--assets",    default=None,
                        help="Komma-getrennte Asset-Liste (z.B. ETH,BTC,SOL)")
    parser.add_argument("--direction", default="long",
                        choices=["both", "long", "short"])
    parser.add_argument("--body",  type=float, default=BODY_ATR_MULT,
                        help=f"Body > X × ATR  (Standard: {BODY_ATR_MULT})")
    parser.add_argument("--vol",   type=float, default=VOL_MULT,
                        help=f"Vol  > X × SMA  (Standard: {VOL_MULT})")
    parser.add_argument("--min-candles", type=int, default=MIN_CANDLES,
                        help=f"Mindest-Kerzen seit Impuls (Standard: {MIN_CANDLES})")
    parser.add_argument("--pb-vol",  type=float, default=PULLBACK_VOL_MAX,
                        help=f"Max Pullback-Vol-Faktor (Standard: {PULLBACK_VOL_MAX})")
    parser.add_argument("--tp",    type=float, default=TP_R,
                        help=f"TP in R (Standard: {TP_R})")
    parser.add_argument("--from",  dest="start", default=IS_START)
    parser.add_argument("--to",    dest="end",   default=IS_END)
    parser.add_argument("--detail", action="store_true",
                        help="Jeden Impuls/Trade/Expire ausgeben")
    args = parser.parse_args()

    assets = args.assets.split(",") if args.assets else DEFAULT_ASSETS
    assets = [a for a in assets
              if os.path.exists(os.path.join(PROJECT_DIR, "data", "historical",
                                             f"{a}_15m.csv"))]

    print(f"\n{'═'*80}")
    print(f"  ZVA Scout — Zeit-Volumen-Asymmetrie  [15m-Chart, LONG-only]")
    print(f"  Body>{args.body}×ATR({ATR_PERIOD})  Vol>{args.vol}×SMA({VOL_SMA_PERIOD})  "
          f"MinK={args.min_candles}(={args.min_candles//4}h)  "
          f"MaxK={MAX_CANDLES}(={MAX_CANDLES//4}h)  "
          f"PbVol<{args.pb_vol}×SMA  TP={args.tp}R")
    print(f"  Zeitraum: {args.start} → {args.end}  |  Richtung: {args.direction.upper()}")
    print(f"{'═'*80}\n")

    all_r  = []
    long_r = []
    short_r= []
    per_asset = {}

    for asset in assets:
        candles = load_csv(asset, "15m")   # direkt 15m, keine Aggregation
        if not candles:
            print(f"  {asset:<6}: keine Daten")
            continue
        if len(candles) < WARMUP + 20:
            print(f"  {asset:<6}: zu wenig Kerzen ({len(candles)})")
            continue

        if args.detail:
            print(f"\n── {asset} {'─'*70}")

        r_long = run_zva(candles, args.start, args.end, "long",
                         args.body, args.vol, args.min_candles,
                         MAX_CANDLES, args.pb_vol, args.tp, args.detail)
        r_asset = r_long

        k = kpis(r_asset)
        per_asset[asset] = {"r": r_asset, "kpis": k}

        if k["n"] == 0:
            print(f"  {asset:<6}: n=0  (keine Setups)")
            continue

        p_tag = "✅" if k["p"] < 0.05 else ("〰" if k["p"] < 0.15 else "  ")
        print(f"  {asset:<6}: n={k['n']:>3}  AvgR={k['avg_r']:>+.3f}  "
              f"WR={k['wr']*100:>4.1f}%  PF={k['pf']:>4.2f}  "
              f"MaxDD={k['max_dd']:.1f}R  p={k['p']:.4f} {p_tag}")

        all_r += r_asset
        long_r += r_long

    # ── Gesamt-Report ─────────────────────────────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  GESAMT LONG-only\n")

    for label, r_list in [("LONG", all_r)]:
        k = kpis(r_list)
        if k["n"] == 0:
            print(f"  {label}: n=0")
            continue
        p_tag = "✅ p<0.05" if k["p"] < 0.05 else ("〰 p<0.15" if k["p"] < 0.15 else f"p={k['p']:.4f}")
        print(f"  {label:<12}:  n={k['n']:>3}  AvgR={k['avg_r']:>+.3f}  "
              f"WR={k['wr']*100:.1f}%  PF={k['pf']:.2f}  "
              f"TotalR={k['total_r']:>+.1f}  MaxDD={k['max_dd']:.1f}R  {p_tag}")

    # R-Verteilung für die beste Richtung
    best_r = all_r
    best_r = long_r
    if best_r:
        print(f"\n  R-Verteilung ({args.direction.upper()}):")
        print(ascii_dist(best_r))

    # ── Gate-0-Check ──────────────────────────────────────────────────────────
    k_all = kpis(all_r)
    print(f"\n{'═'*80}")
    print(f"  GATE 0 — Scout\n")
    gates = [
        ("n ≥ 30",     k_all["n"] >= 30,     f"n={k_all['n']}"),
        ("Avg R > 0",  k_all["avg_r"] > 0,   f"Avg R={k_all['avg_r']:+.3f}"),
        ("p < 0.05",   k_all["p"] < 0.05,    f"p={k_all['p']:.4f}"),
        ("WR > 30%",   k_all["wr"] > 0.30,   f"WR={k_all['wr']*100:.1f}%"),
    ]
    passed = 0
    for name, ok, val in gates:
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name:<15}  {val}")
        if ok: passed += 1

    verdict = "GO → WFA starten" if passed == 4 else \
              f"NO-GO ({passed}/4) → Parameter anpassen oder Stop"
    print(f"\n  Verdikt: {'✅ ' if passed==4 else '❌ '}{verdict}")
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
