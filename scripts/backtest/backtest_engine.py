#!/usr/bin/env python3
"""
APEX ORB Backtest-Engine — simuliert die Strategie auf historischen Candle-Daten.

Repliziert 1:1 die Logik aus:
  - save_opening_range.py   (Box-Berechnung)
  - autonomous_trade.py     (11-Filter-Kette, Breakout-Detection)
  - skip_counterfactual.py  (Trade-Simulation: TP1/TP2/BE, SL-first)
  - position_monitor.py     (BE-Mechanik)

Gibt eine Trade-Liste zurück die direkt in filter_attribution.py geladen werden kann.

Nicht simuliert:
  - Live-API-Calls (Regime-Detector nutzt BTC-Candles direkt)
  - Slippage auf Orderbook-Tiefe-Basis (Pauschale TAKER_FEE + SLIPPAGE_PCT)
  - Funding-Rate (Pauschale FUNDING_RATE_PER_8H)
"""
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from config.bot_config import (
    ASSETS, BREAKOUT_THRESHOLD, MIN_BOX_RANGE, MAX_BOX_AGE_MIN,
    MAX_BREAKOUT_DISTANCE_RATIO, H006_EMA_FILTER_ENABLED,
    H006_REQUIRE_H4_ALIGN, H014_VOLUME_FILTER_ENABLED, H014_VOLUME_RATIO_MIN,
)

BERLIN = ZoneInfo("Europe/Berlin")
HIST_DIR = os.path.join(PROJECT_DIR, "data", "historical")

# Session-Definitionen: (Box-Close Stunde, Box-Close Minute, Scan-Start Stunde, Scan-Ende Stunde)
SESSIONS = {
    "tokyo": {"box_h": 2,  "box_m": 15, "scan_start": 2,  "scan_end": 4},
    "eu":    {"box_h": 9,  "box_m": 15, "scan_start": 9,  "scan_end": 11},
    "us":    {"box_h": 21, "box_m": 15, "scan_start": 21, "scan_end": 23},
}

# Fee-Modell (Bitget USDT-Futures, VIP-0)
TAKER_FEE         = 0.0006   # 0.06% pro Entry + Exit
FUNDING_PER_8H    = 0.0001   # 0.01%/8h pauschal (konservativ)
SLIPPAGE_PCT      = 0.0005   # 0.05% Entry-Slippage

# Backtest-Parameter (gespiegelt aus bot_config, aber togglebar)
BODY_STRENGTH_MIN = 0.30     # H-009 / always-on: |close-open| / (high-low)
VOLUME_PERIOD     = 20       # H-014: SMA-Periode für Volume-Ratio
EMA_WARMUP        = 210      # Candles für EMA-200 Berechnung
ATR_PERIOD        = 14


# ─────────────────────────────────────────────────────────────
# Candle-Loader
# ─────────────────────────────────────────────────────────────

def load_candles_csv(asset: str, interval: str) -> list[dict]:
    """Lädt CSV aus data/historical/ als Liste von dicts."""
    path = os.path.join(HIST_DIR, f"{asset}_{interval}.csv")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "time": int(row["time_ms"]),
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
    rows.sort(key=lambda x: x["time"])
    return rows


def candles_at(candles: list[dict], ts_ms: int) -> dict | None:
    """Binäre Suche: gibt Candle zurück die zum Zeitpunkt ts_ms aktiv war."""
    lo, hi = 0, len(candles) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if candles[mid]["time"] == ts_ms:
            return candles[mid]
        elif candles[mid]["time"] < ts_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    # Letzten Candle vor ts_ms
    if hi >= 0:
        return candles[hi]
    return None


def candles_before(candles: list[dict], ts_ms: int, n: int) -> list[dict]:
    """Gibt die n Candles zurück die vor ts_ms enden (älteste zuerst)."""
    result = []
    for c in candles:
        if c["time"] < ts_ms:
            result.append(c)
        else:
            break
    return result[-n:] if len(result) >= n else result


def candles_after(candles: list[dict], ts_ms: int, n: int) -> list[dict]:
    """Gibt n Candles ab ts_ms (inklusive) zurück."""
    result = []
    for c in candles:
        if c["time"] >= ts_ms:
            result.append(c)
            if len(result) >= n:
                break
    return result


# ─────────────────────────────────────────────────────────────
# Technische Indikatoren (pure Python, keine Dependencies)
# ─────────────────────────────────────────────────────────────

def calc_ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_atr(candles: list, period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_sma(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def calc_stdev(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    subset = values[-period:]
    mean = sum(subset) / period
    return math.sqrt(sum((x - mean) ** 2 for x in subset) / period)


def calc_trend_context(candles_15m: list) -> dict:
    """Berechnet EMA-200, EMA-50, ATR-14, Squeeze-Flag — aus den letzten N 15m-Candles."""
    if len(candles_15m) < EMA_WARMUP:
        return {}
    closes = [c["close"] for c in candles_15m]
    ema_200 = calc_ema(closes, 200)
    ema_50  = calc_ema(closes, 50)
    atr_14  = calc_atr(candles_15m, ATR_PERIOD)
    last_close = closes[-1]

    # Squeeze (H-013): BB ⊂ KC
    sma_20   = calc_sma(closes, 20)
    stdev_20 = calc_stdev(closes, 20)
    bb_upper = sma_20 + 2.0 * stdev_20
    bb_lower = sma_20 - 2.0 * stdev_20
    kc_upper = sma_20 + 1.5 * atr_14
    kc_lower = sma_20 - 1.5 * atr_14
    is_squeezing = (bb_upper < kc_upper) and (bb_lower > kc_lower)
    atr_ratio = (atr_14 / sma_20) if sma_20 else 0.0

    trend_direction = "above" if last_close > ema_200 else "below"
    return {
        "ema_200": round(ema_200, 6),
        "ema_50":  round(ema_50, 6),
        "atr_14":  round(atr_14, 6),
        "trend_direction": trend_direction,
        "is_squeezing": is_squeezing,
        "atr_ratio": round(atr_ratio, 4),
    }


# ─────────────────────────────────────────────────────────────
# Filter-Kette (pure Functions, alle togglebar)
# ─────────────────────────────────────────────────────────────

def filter_check_breakout(asset: str, price: float, box_high: float, box_low: float):
    """Gibt 'long' | 'short' | None zurück."""
    threshold = BREAKOUT_THRESHOLD.get(asset, price * 0.002)
    if price > box_high + threshold:
        return "long"
    elif price < box_low - threshold:
        return "short"
    return None


def filter_late_entry(direction: str, price: float, box_high: float, box_low: float,
                      box_range: float) -> bool:
    """True = Skip (zu weit weg)."""
    box_level = box_high if direction == "long" else box_low
    dist = abs(price - box_level)
    return dist > MAX_BREAKOUT_DISTANCE_RATIO * box_range


def filter_candle_confirmed(direction: str, candle_close: float,
                            box_high: float, box_low: float) -> bool:
    """True = Skip (Candle-Close nicht jenseits der Box-Grenze)."""
    if direction == "long":
        return candle_close <= box_high
    return candle_close >= box_low


def filter_weak_candle(candle: dict) -> bool:
    """True = Skip (Doji/Spinning-Top: body_ratio < 30%)."""
    body = abs(candle["close"] - candle["open"])
    wick = candle["high"] - candle["low"]
    if wick == 0:
        return False
    return (body / wick) < BODY_STRENGTH_MIN


def filter_ema_aligned(direction: str, close: float, ema_200: float,
                       ema_50_4h: float | None, enabled: bool, require_h4: bool) -> tuple[bool, bool]:
    """
    Gibt (ema_aligned, h4_aligned) zurück.
    filter_skip = True wenn EMA-Filter aktiv UND nicht aligned.
    """
    if not enabled or ema_200 == 0:
        return True, True
    ema_aligned = (close > ema_200) if direction == "long" else (close < ema_200)
    h4_aligned = True
    if require_h4 and ema_50_4h:
        h4_aligned = (close > ema_50_4h) if direction == "long" else (close < ema_50_4h)
    return ema_aligned, h4_aligned


def filter_volume(volume: float, volume_history: list, enabled: bool, min_ratio: float) -> tuple[bool, float]:
    """Gibt (skip, volume_ratio) zurück. skip=True wenn Volumen zu gering."""
    if not enabled or len(volume_history) < VOLUME_PERIOD:
        return False, 0.0
    avg = calc_sma(volume_history, VOLUME_PERIOD)
    ratio = volume / avg if avg > 0 else 0.0
    return ratio < min_ratio, round(ratio, 3)


# ─────────────────────────────────────────────────────────────
# Trade-Simulation (aus skip_counterfactual.simulate())
# ─────────────────────────────────────────────────────────────

def simulate_trade(direction: str, entry: float, box_high: float, box_low: float,
                   candles_5m: list, fee_model: bool = True) -> dict:
    """
    Simuliert einen Trade auf 5m-Candles.
    SL-first conservative bei gleicher Candle (wie in skip_counterfactual.py).
    Returns: {r_outcome, exit_reason, bars_to_exit, net_r_outcome}
    """
    box_range = box_high - box_low
    sl_buffer = max(box_range * 0.1, entry * 0.001)

    if direction == "long":
        sl  = box_low - sl_buffer
        r   = entry - sl
        tp1 = entry + r * 1.0
        tp2 = entry + r * 3.0
    else:
        sl  = box_high + sl_buffer
        r   = sl - entry
        tp1 = entry - r * 1.0
        tp2 = entry - r * 3.0

    if r <= 0:
        return {"r_outcome": None, "exit_reason": "invalid_setup", "bars_to_exit": 0, "net_r_outcome": None}

    tp1_hit  = False
    active_sl = sl

    for i, c in enumerate(candles_5m):
        h, l = c["high"], c["low"]

        if direction == "long":
            if l <= active_sl:
                raw_r = 0.5 * 1.0 + 0.5 * 0.0 if tp1_hit else -1.0
                reason = "be_after_tp1" if tp1_hit else "sl"
                return _exit(raw_r, reason, i + 1, entry, r, fee_model)
            if not tp1_hit and h >= tp1:
                tp1_hit = True
                active_sl = entry
                if h >= tp2:
                    return _exit(0.5 * 1.0 + 0.5 * 3.0, "tp2", i + 1, entry, r, fee_model)
            elif tp1_hit and h >= tp2:
                return _exit(0.5 * 1.0 + 0.5 * 3.0, "tp2", i + 1, entry, r, fee_model)
        else:
            if h >= active_sl:
                raw_r = 0.5 * 1.0 + 0.5 * 0.0 if tp1_hit else -1.0
                reason = "be_after_tp1" if tp1_hit else "sl"
                return _exit(raw_r, reason, i + 1, entry, r, fee_model)
            if not tp1_hit and l <= tp1:
                tp1_hit = True
                active_sl = entry
                if l <= tp2:
                    return _exit(0.5 * 1.0 + 0.5 * 3.0, "tp2", i + 1, entry, r, fee_model)
            elif tp1_hit and l <= tp2:
                return _exit(0.5 * 1.0 + 0.5 * 3.0, "tp2", i + 1, entry, r, fee_model)

    # Timeout: Mark-to-Market auf letztem Close
    if not candles_5m:
        return {"r_outcome": None, "exit_reason": "no_data", "bars_to_exit": 0, "net_r_outcome": None}

    last_close = candles_5m[-1]["close"]
    mtm_r = (last_close - entry) / r if direction == "long" else (entry - last_close) / r
    if tp1_hit:
        raw_r = 0.5 * 1.0 + 0.5 * mtm_r
        reason = "timeout_after_tp1"
    else:
        raw_r = mtm_r
        reason = "timeout"
    return _exit(round(raw_r, 3), reason, len(candles_5m), entry, r, fee_model)


def _exit(raw_r: float, reason: str, bars: int, entry: float, r_size: float, fee_model: bool) -> dict:
    """Berechnet net_r_outcome nach Abzug von Fees + Slippage."""
    net_r = raw_r
    if fee_model and r_size > 0:
        # Fee-Kosten in R-Einheiten ausgedrückt: fee_usd / r_usd
        # Slippage nur beim Entry; Taker-Fee bei Entry + Exit
        slippage_r = (entry * SLIPPAGE_PCT) / r_size
        taker_r    = (entry * TAKER_FEE * 2) / r_size  # Entry + Exit
        # Holding-Dauer grob: bars * 5min / 60 / 8h * funding
        funding_r  = 0.0  # Vernachlässigbar bei <4h Haltezeit
        net_r = round(raw_r - slippage_r - taker_r - funding_r, 3)
    return {
        "r_outcome":     round(raw_r, 3),
        "net_r_outcome": net_r,
        "exit_reason":   reason,
        "bars_to_exit":  bars,
    }


# ─────────────────────────────────────────────────────────────
# Hauptklasse: ORBBacktester
# ─────────────────────────────────────────────────────────────

class ORBBacktester:
    def __init__(self, assets=None, filters_off: set = None, fee_model: bool = True,
                 verbose: bool = True):
        self.assets      = assets or ASSETS
        self.filters_off = filters_off or set()   # z.B. {"ema200_misaligned", "low_volume"}
        self.fee_model   = fee_model
        self.verbose     = verbose

        # Candles laden (15m für Box + Indikatoren, 5m für Trade-Simulation)
        self.candles_15m = {}
        self.candles_5m  = {}
        self.candles_4h  = {}
        for asset in self.assets:
            self.candles_15m[asset] = load_candles_csv(asset, "15m")
            self.candles_5m[asset]  = load_candles_csv(asset, "5m")
            self.candles_4h[asset]  = load_candles_csv(asset, "4h")
            if verbose:
                print(f"   📂 {asset}: {len(self.candles_15m[asset])} × 15m, "
                      f"{len(self.candles_5m[asset])} × 5m, "
                      f"{len(self.candles_4h[asset])} × 4h")

        # BTC 4H für Regime (separat)
        self.btc_4h = load_candles_csv("BTC", "4h")
        if verbose:
            print(f"   📂 BTC: {len(self.btc_4h)} × 4h (Regime)")

    def run(self, start_date: str, end_date: str) -> dict:
        """
        Walk-forward über alle Sessions im Zeitraum.
        Returns: {"trades": [...], "skips": [...], "summary": {...}}
        """
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=BERLIN)
        end_dt   = datetime.strptime(end_date,   "%Y-%m-%d").replace(tzinfo=BERLIN)

        trades = []
        skips  = []
        session_trades = {}  # {(date, asset, session): True} für already_traded Check

        current = start_dt
        while current <= end_dt:
            # Wochentag: 0=Mo..6=So → nur Mo-Fr traden
            if current.weekday() < 5:
                for session_name, sess in SESSIONS.items():
                    box_ts = current.replace(
                        hour=sess["box_h"], minute=sess["box_m"], second=0, microsecond=0
                    )
                    self._run_session(
                        box_ts, session_name, trades, skips, session_trades
                    )
            current += timedelta(days=1)

        summary = self._compute_summary(trades, skips)
        return {"trades": trades, "skips": skips, "summary": summary}

    def _run_session(self, box_ts: datetime, session: str,
                     trades: list, skips: list, session_trades: dict):
        """Simuliert eine komplette Session (Box berechnen + Scan-Schritte)."""
        box_ms = int(box_ts.timestamp() * 1000)

        # Box: 15m-Candle die bei box_ts geschlossen hat
        boxes = {}
        for asset in self.assets:
            c15 = self.candles_15m.get(asset, [])
            # Box = letzte geschlossene 15m-Candle vor box_ts
            prev_candles = candles_before(c15, box_ms, 5)
            if not prev_candles:
                continue
            box_candle = prev_candles[-1]
            box_range = box_candle["high"] - box_candle["low"]
            if box_range < MIN_BOX_RANGE.get(asset, 0):
                skips.append({"session": session, "asset": asset, "reason": "box_too_small",
                               "ts": box_ts.isoformat()})
                continue
            boxes[asset] = {
                "high": box_candle["high"],
                "low":  box_candle["low"],
                "ts_ms": box_ms,
                "range": box_range,
            }

        if not boxes:
            return

        # Scan: alle 5m-Schritte im Session-Fenster
        sess = SESSIONS[session]
        scan_start = box_ts.replace(hour=sess["scan_start"], minute=0)
        scan_end   = box_ts.replace(hour=sess["scan_end"],   minute=0)
        scan_ts    = scan_start

        while scan_ts < scan_end:
            scan_ms = int(scan_ts.timestamp() * 1000)
            date_key = scan_ts.date().isoformat()

            for asset in self.assets:
                if asset not in boxes:
                    continue

                trade_key = (date_key, asset, session)
                if trade_key in session_trades:
                    continue  # already_traded

                box = boxes[asset]
                c5  = self.candles_5m.get(asset, [])
                c15 = self.candles_15m.get(asset, [])
                c4h = self.candles_4h.get(asset, [])

                # Aktuelle 5m-Candle
                curr_5m = candles_at(c5, scan_ms)
                if curr_5m is None:
                    continue

                current_price = curr_5m["close"]

                # Breakout-Check
                direction = filter_check_breakout(asset, current_price, box["high"], box["low"])
                if direction is None:
                    skips.append({"session": session, "asset": asset, "reason": "no_breakout",
                                  "ts": scan_ts.isoformat()})
                    scan_ts += timedelta(minutes=5)
                    continue

                # Box-Alter
                box_age_min = (scan_ms - box["ts_ms"]) / 60000
                if box_age_min > MAX_BOX_AGE_MIN:
                    skips.append({"session": session, "asset": asset, "reason": "box_too_old",
                                  "ts": scan_ts.isoformat()})
                    continue

                # Late-Entry
                if "late_entry" not in self.filters_off:
                    if filter_late_entry(direction, current_price, box["high"], box["low"], box["range"]):
                        skips.append({"session": session, "asset": asset, "reason": "late_entry",
                                      "ts": scan_ts.isoformat(),
                                      "context": {"direction": direction, "entry_price": current_price,
                                                  "box_high": box["high"], "box_low": box["low"]}})
                        continue

                # Candle-Confirmation
                if filter_candle_confirmed(direction, curr_5m["close"], box["high"], box["low"]):
                    skips.append({"session": session, "asset": asset, "reason": "candle_not_confirmed",
                                  "ts": scan_ts.isoformat()})
                    continue

                # Weak Candle
                if "weak_candle" not in self.filters_off:
                    if filter_weak_candle(curr_5m):
                        skips.append({"session": session, "asset": asset, "reason": "weak_candle",
                                      "ts": scan_ts.isoformat(),
                                      "context": {"direction": direction, "entry_price": current_price,
                                                  "box_high": box["high"], "box_low": box["low"]}})
                        continue

                # Volume (H-014)
                vol_history_candles = candles_before(c5, scan_ms, VOLUME_PERIOD + 1)
                vol_history = [c["volume"] for c in vol_history_candles]
                skip_vol, vol_ratio = filter_volume(
                    curr_5m["volume"], vol_history,
                    H014_VOLUME_FILTER_ENABLED and "low_volume" not in self.filters_off,
                    H014_VOLUME_RATIO_MIN
                )
                if skip_vol:
                    skips.append({"session": session, "asset": asset, "reason": "low_volume",
                                  "ts": scan_ts.isoformat(),
                                  "context": {"direction": direction, "entry_price": current_price,
                                              "box_high": box["high"], "box_low": box["low"],
                                              "volume_ratio": vol_ratio}})
                    continue

                # EMA-Filter (H-006)
                ctx_candles = candles_before(c15, scan_ms, EMA_WARMUP)
                trend_ctx   = calc_trend_context(ctx_candles) if len(ctx_candles) >= EMA_WARMUP else {}
                ema_200  = trend_ctx.get("ema_200", 0)
                c4h_prev = candles_before(c4h, scan_ms, 60)
                ema_50_4h = calc_ema([c["close"] for c in c4h_prev], 50) if len(c4h_prev) >= 50 else None

                ema_aligned, h4_aligned = filter_ema_aligned(
                    direction, current_price, ema_200, ema_50_4h,
                    H006_EMA_FILTER_ENABLED and "ema200_misaligned" not in self.filters_off,
                    H006_REQUIRE_H4_ALIGN and "ema200_h4_misaligned" not in self.filters_off,
                )
                if not ema_aligned:
                    skips.append({"session": session, "asset": asset, "reason": "ema200_misaligned",
                                  "ts": scan_ts.isoformat(),
                                  "context": {"direction": direction, "entry_price": current_price,
                                              "box_high": box["high"], "box_low": box["low"],
                                              "ema_200": ema_200}})
                    continue
                if not h4_aligned:
                    skips.append({"session": session, "asset": asset, "reason": "ema200_h4_misaligned",
                                  "ts": scan_ts.isoformat(),
                                  "context": {"direction": direction, "entry_price": current_price,
                                              "box_high": box["high"], "box_low": box["low"]}})
                    continue

                # ─── Trade auslösen ───
                entry_price = current_price * (1 + SLIPPAGE_PCT) if direction == "long" \
                              else current_price * (1 - SLIPPAGE_PCT)

                # Nächste 48 × 5m-Candles für Trade-Simulation (4h Fenster)
                future_candles = candles_after(c5, scan_ms + 5 * 60 * 1000, 48)
                sim = simulate_trade(direction, entry_price, box["high"], box["low"],
                                     future_candles, self.fee_model)

                session_trades[trade_key] = True

                body_ratio = (abs(curr_5m["close"] - curr_5m["open"]) /
                              (curr_5m["high"] - curr_5m["low"])) if (curr_5m["high"] - curr_5m["low"]) > 0 else 0

                trade = {
                    "timestamp":     scan_ts.isoformat(),
                    "asset":         asset,
                    "session":       session,
                    "direction":     direction,
                    "entry_price":   round(entry_price, 6),
                    "box_high":      box["high"],
                    "box_low":       box["low"],
                    "box_range":     round(box["range"], 6),
                    "exit_pnl_r":    sim["net_r_outcome"],
                    "exit_reason":   sim["exit_reason"],
                    "bars_to_exit":  sim["bars_to_exit"],
                    "gross_r":       sim["r_outcome"],
                    "body_ratio":    round(body_ratio, 3),
                    "volume_ratio":  vol_ratio,
                    "trend_context": {
                        "ema_aligned": ema_aligned,
                        "h4_aligned":  h4_aligned,
                        "ema_200":     ema_200,
                        "atr_14":      trend_ctx.get("atr_14", 0),
                        "is_squeezing": trend_ctx.get("is_squeezing", False),
                    },
                    "_source": "backtest",
                }
                trades.append(trade)

                if self.verbose:
                    r_str = f"{sim['net_r_outcome']:+.2f}R" if sim["net_r_outcome"] is not None else "None"
                    print(f"   {scan_ts.strftime('%Y-%m-%d')} {session:<5} {asset:<4} "
                          f"{direction:<5} → {r_str} ({sim['exit_reason']})")

            scan_ts += timedelta(minutes=5)

    def _compute_summary(self, trades: list, skips: list) -> dict:
        closed = [t for t in trades if t.get("exit_pnl_r") is not None]
        if not closed:
            return {"n_trades": 0, "n_skips": len(skips)}
        r_vals = [t["exit_pnl_r"] for t in closed]
        wins   = [r for r in r_vals if r > 0]
        losses = [r for r in r_vals if r < 0]
        win_rate = len(wins) / len(r_vals)
        avg_r    = sum(r_vals) / len(r_vals)
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(wins) > 0 else float("inf")

        # Equity-Kurve + Max-Drawdown
        equity = 0.0
        peak   = 0.0
        max_dd = 0.0
        for r in r_vals:
            equity += r
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        skip_reasons = {}
        for s in skips:
            skip_reasons[s["reason"]] = skip_reasons.get(s["reason"], 0) + 1

        return {
            "n_trades":    len(closed),
            "n_skips":     len(skips),
            "win_rate":    round(win_rate, 3),
            "avg_r":       round(avg_r, 3),
            "total_r":     round(sum(r_vals), 2),
            "profit_factor": round(pf, 2),
            "max_drawdown_r": round(max_dd, 2),
            "skip_reasons": skip_reasons,
        }
