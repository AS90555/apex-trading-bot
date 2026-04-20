#!/usr/bin/env python3
"""
APEX - Autonomous Trading Script
=================================
Wird von Cron Jobs aufgerufen, checkt Breakouts, platziert Orders autonom.
"""

import os
import sys
import json
import fcntl
import time
import math
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient, MIN_TRADE_SIZE
from telegram_sender import send_telegram_message

# Config laden
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import (
        DRY_RUN, CAPITAL, MAX_RISK_PCT, ASSET_PRIORITY,
        BREAKOUT_THRESHOLD, LEVERAGE, SIZE_DECIMALS, DRAWDOWN_KILL_PCT,
        MIN_BOX_RANGE, MAX_BOX_AGE_MIN, MAX_BREAKOUT_DISTANCE_RATIO,
        H006_EMA_FILTER_ENABLED, H006_REQUIRE_H4_ALIGN,
        H014_VOLUME_FILTER_ENABLED, H014_VOLUME_RATIO_MIN,
        H015_REGIME_RISK_MODIFIER_ENABLED,
        MIN_BALANCE_USD, MAX_SL_DISTANCE_PCT, DAILY_DD_KILL_R, DAILY_DD_HALF_R,
    )
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0
    MAX_RISK_PCT = 0.02
    ASSET_PRIORITY = ["ETH", "SOL", "AVAX", "XRP"]
    BREAKOUT_THRESHOLD = {"ETH": 5.0, "SOL": 0.30, "AVAX": 0.15, "XRP": 0.001}
    LEVERAGE = 5
    SIZE_DECIMALS = {"ETH": 2, "SOL": 1, "AVAX": 1, "XRP": 0}
    DRAWDOWN_KILL_PCT = 0.50
    MIN_BOX_RANGE = {"ETH": 1.0, "SOL": 0.10, "AVAX": 0.04, "XRP": 0.003}
    MAX_BOX_AGE_MIN = 120
    MAX_BREAKOUT_DISTANCE_RATIO = 2.0
    H006_EMA_FILTER_ENABLED = False
    H006_REQUIRE_H4_ALIGN = False
    H014_VOLUME_FILTER_ENABLED = False
    H014_VOLUME_RATIO_MIN = 1.0
    H015_REGIME_RISK_MODIFIER_ENABLED = False
    DAILY_DD_HALF_R = -1.5
    MIN_BALANCE_USD = 10.0
    MAX_SL_DISTANCE_PCT = 0.10
    DAILY_DD_KILL_R = -2.0

MAX_RISK_USD = CAPITAL * MAX_RISK_PCT

# Datenpfade
BOXES_FILE = os.path.join(DATA_DIR, "opening_range_boxes.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
LOCK_FILE = os.path.join(DATA_DIR, "autonomous_trade.lock")
HWM_FILE = os.path.join(DATA_DIR, "high_water_mark.json")
SKIP_LOG_FILE   = os.path.join(DATA_DIR, "skip_log.jsonl")
H011_SHADOW_FILE = os.path.join(DATA_DIR, "hypothesis_shadow_log.jsonl")


def log_skip(reason: str, asset: str = None, session: str = None, context: dict = None):
    """Append strukturierter Skip-Eintrag in data/skip_log.jsonl (append-only JSONL).

    reason: position_open | box_too_old | box_missing_ts | box_too_small | price_fetch_fail |
            no_breakout | late_entry | candle_not_confirmed | already_traded |
            kill_switch | no_session
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(),
            "session": session if session is not None else get_current_session(),
            "asset": asset,
            "reason": reason,
            "context": context or {},
        }
        with open(SKIP_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"⚠️  Skip-Log Schreibfehler: {e}")


def log_h011_shadow(asset, direction, entry_price, stop_loss, trend_context: dict):
    """H-011: ATR-Trail Shadow-Log — was would the trail look like if activated at 1R?"""
    try:
        atr = trend_context.get("atr_14", 0)
        risk = abs(entry_price - stop_loss)
        tp1_price = entry_price + risk if direction == "long" else entry_price - risk
        trail_1x = atr * 1.0
        trail_15x = atr * 1.5
        entry = {
            "timestamp": datetime.now().isoformat(),
            "hypothesis": "H-011",
            "asset": asset,
            "direction": direction,
            "entry_price": round(entry_price, 6),
            "stop_loss": round(stop_loss, 6),
            "risk_per_unit": round(risk, 6),
            "atr_14": round(atr, 6),
            "atr_pct": round(atr / entry_price * 100, 4) if entry_price else None,
            "tp1_activation_price": round(tp1_price, 6),
            "would_trail_1x_atr": round(trail_1x, 6),
            "would_trail_15x_atr": round(trail_15x, 6),
            "trail_vs_risk_ratio": round(trail_1x / risk, 3) if risk else None,
        }
        with open(H011_SHADOW_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"⚠️  H-011 Shadow-Log Fehler: {e}")


def log_h012_h013_shadow(asset, session, direction, current_price,
                          trend_context: dict, or_bias: str, bias_aligned: bool,
                          box_mid: float, prev_mid):
    """H-012 + H-013: Shadow-Log für JEDEN Breakout-Kandidaten (inkl. gefilterter Trades).

    Wird VOR den Filtern (H-006/H-009/H-014) aufgerufen damit auch geblockte Signale erfasst werden.
    Später Join mit skip_log.jsonl oder trades.json über (asset, timestamp).
    """
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "asset": asset,
            "session": session,
            "direction": direction,
            "current_price": round(current_price, 6),
            "h013_is_squeezing": trend_context.get("is_squeezing", None),
            "h013_atr_ratio": trend_context.get("atr_ratio", None),
            "h012_or_bias": or_bias,
            "h012_bias_aligned": bias_aligned,
            "h012_box_mid": round(box_mid, 6) if box_mid is not None else None,
            "h012_prev_mid": round(prev_mid, 6) if prev_mid is not None else None,
            "ema_aligned": trend_context.get("ema_aligned", None),
            "h4_aligned": trend_context.get("h4_aligned", None),
        }
        with open(H011_SHADOW_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"⚠️  H-012/H-013 Shadow-Log Fehler: {e}")


def load_boxes():
    if not os.path.exists(BOXES_FILE):
        return {}
    try:
        with open(BOXES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  Boxes-Datei korrupt oder unlesbar: {e}")
        return {}


def get_current_session():
    """Bestimme aktuelle Trading-Session (Berlin-Zeit)"""
    now = datetime.now()
    hour = now.hour
    if 2 <= hour < 4:
        return "tokyo"
    elif 9 <= hour < 11:
        return "eu"
    elif 21 <= hour < 23:
        return "us"
    return None


def has_traded_today_in_session(session):
    """Prüfe ob in dieser Session heute schon getradet wurde (newest-first für Performance)"""
    if not os.path.exists(TRADES_FILE):
        return False

    try:
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  trades.json unlesbar: {e}")
        return False

    today = datetime.now().date().isoformat()
    for trade in reversed(trades):
        trade_date = trade.get("timestamp", "")[:10]
        # Optimierung: ältere Trades als heute → kein Match mehr möglich, abbrechen
        if trade_date and trade_date < today:
            break
        trade_session = trade.get("session", "")
        if not trade_session:
            trade_hour = datetime.fromisoformat(trade["timestamp"]).hour
            if 2 <= trade_hour < 4:
                trade_session = "tokyo"
            elif 9 <= trade_hour < 11:
                trade_session = "eu"
            elif 21 <= trade_hour < 23:
                trade_session = "us"
        if trade_date == today and trade_session == session:
            return True
    return False


def log_trade(trade_data):
    """Logge Trade in trades.json"""
    os.makedirs(DATA_DIR, exist_ok=True)
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                trades = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  trades.json unlesbar in log_trade: {e}")
            trades = []

    trades.append({
        **trade_data,
        "timestamp": datetime.now().isoformat(),
        "session": get_current_session(),
    })

    tmp_file = TRADES_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp_file, TRADES_FILE)


def _fetch_fear_and_greed() -> dict:
    """Holt den aktuellen Fear & Greed Index von alternative.me (kostenlos, kein API-Key).
    Returns: {"value": int, "label": str} z.B. {"value": 72, "label": "Greed"}
    Nur für Logging — kein Filter. Bei Fehler: leeres Dict.
    """
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "APEX-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        entry = data["data"][0]
        return {
            "value": int(entry["value"]),
            "label": entry["value_classification"],
        }
    except Exception:
        return {}


def _calc_ema(closes: list, period: int) -> float:
    """Exponential Moving Average (Standard EMA, k=2/(period+1))."""
    if len(closes) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _calc_atr(candles: list, period: int = 14) -> float:
    """Average True Range mit Wilder-Smoothing (RMA)."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _calc_sma(closes: list, period: int) -> float:
    """Simple Moving Average (Arithmetisches Mittel der letzten N Closes)."""
    if len(closes) < period:
        return 0.0
    return sum(closes[-period:]) / period


def _calc_stdev(closes: list, period: int) -> float:
    """Standardabweichung (Volatilität) über die letzten N Closes."""
    if len(closes) < period:
        return 0.0
    subset = closes[-period:]
    mean = sum(subset) / period
    variance = sum((x - mean) ** 2 for x in subset) / period
    return math.sqrt(variance)


def check_breakout(asset, current_price, box_high, box_low):
    """
    Prüfe ob Asset aus Box ausgebrochen ist.
    Returns: "long" | "short" | None
    """
    threshold = BREAKOUT_THRESHOLD.get(asset, current_price * 0.002)
    if current_price > box_high + threshold:
        return "long"
    elif current_price < box_low - threshold:
        return "short"
    return None


def round_size(asset, size):
    """Runde Size auf Bitget-konforme Dezimalstellen"""
    decimals = SIZE_DECIMALS.get(asset, 2)
    return round(size, decimals)


def execute_breakout_trade(client, asset, direction, entry_price, box_high, box_low, risk_usd=None, context=None, regime_snapshot=None):
    """
    Platziere Breakout Trade mit Stop-Loss und Take-Profit.
    Returns: dict mit Trade-Ergebnis
    """
    # Stop-Loss: dynamischer Puffer basierend auf Box-Größe
    box_range = box_high - box_low
    sl_buffer = max(box_range * 0.1, entry_price * 0.001)  # 10% der Box oder 0.1% vom Preis
    if direction == "long":
        stop_loss = box_low - sl_buffer
    else:
        stop_loss = box_high + sl_buffer

    # Breakout-Trigger-Preis (Box-Grenze + Threshold) – Basis für Slippage-Messung
    bo_threshold = BREAKOUT_THRESHOLD.get(asset, entry_price * 0.002)
    trigger_price = (box_high + bo_threshold) if direction == "long" else (box_low - bo_threshold)

    # Risk: live Balance verwenden falls übergeben, sonst Fallback
    effective_risk = risk_usd if risk_usd is not None else MAX_RISK_USD

    # Position Size: Risk / Stop-Distanz
    size = client.calculate_position_size(effective_risk, entry_price, stop_loss)
    size = round_size(asset, size)

    if size <= 0:
        return {"success": False, "error": "Berechnete Size zu klein"}

    # Margin-Cap: max. 90% des Kontos als Margin (verhindert "Insufficient margin" bei Bitget)
    balance_est = effective_risk / MAX_RISK_PCT
    max_size_by_margin = (balance_est * 0.90 * LEVERAGE) / entry_price
    max_size_by_margin = round_size(asset, max_size_by_margin)
    if size > max_size_by_margin and max_size_by_margin > 0:
        print(f"   ⚠️  Size {size} → {max_size_by_margin} gekappt (Margin-Limit 90%)")
        size = max_size_by_margin

    # Leverage setzen
    client.set_leverage(asset, LEVERAGE)

    is_buy = (direction == "long")
    hold_side = "long" if is_buy else "short"

    # Orphan-Order Cleanup: verbleibende SL/TP-Orders vom letzten Trade löschen
    # (Bitget cancelt die Gegenseite nicht automatisch wenn TP/SL triggert)
    print(f"   🧹 Bereinige verbleibende TP/SL-Orders für {asset}...")
    cancel_ok = client.cancel_tpsl_orders(asset)
    if not cancel_ok and not DRY_RUN:
        # Einmal retry nach 3s
        time.sleep(3)
        cancel_ok = client.cancel_tpsl_orders(asset)
    if not cancel_ok and not DRY_RUN:
        alert = f"Cancel für bestehende {asset} Orders hat nicht funktioniert — Trade abgebrochen."
        print(f"\n{alert}")
        send_telegram_message(alert)
        return {"success": False, "error": "Orphan-Order Cancel fehlgeschlagen"}

    # Market Order mit Preset-SL als Notfall-Netz (kein TP — wird separat als Split gesetzt)
    order_result = client.place_market_order(
        coin=asset,
        is_buy=is_buy,
        size=size,
        reduce_only=False,
        stop_loss=stop_loss,
        take_profit=None,
    )

    if not order_result.success:
        return {"success": False, "error": order_result.error}

    actual_entry = order_result.avg_price

    # Market-Structure beim Entry: OI + OI-Delta, Long-Account-%, Taker-Buy-Ratio, Funding Rate.
    # Nur geloggt – kein Filter. Datenbasis für Analyse nach 30 Trades.
    market_structure = {}
    try:
        oi_now = client.get_open_interest(asset)
        # OI-Delta: aktueller OI vs. letzter verfügbarer 5-min-Historyeintrag
        oi_delta = None
        oi_delta_pct = None
        try:
            oi_hist = client.get_open_interest_history(asset, period="5m", limit=2)
            if len(oi_hist) >= 2 and oi_hist[0]["oi"] > 0:
                oi_prev = oi_hist[0]["oi"]
                oi_delta = round(oi_now - oi_prev, 2)
                oi_delta_pct = round((oi_delta / oi_prev) * 100, 3)
        except Exception:
            pass
        market_structure = {
            "open_interest":    oi_now,
            "oi_delta":         oi_delta,         # absolut (Kontrakte) — positiv = steigendes OI = echte neue Positionen
            "oi_delta_pct":     oi_delta_pct,     # relativ in % — z.B. +1.2 = +1.2% OI-Anstieg
            "long_account_pct": client.get_long_account_ratio(asset),   # 0.68 = 68% Long-Accounts
            "taker_buy_ratio":  client.get_taker_ratio(asset),           # >0.55 bullish, <0.45 bearish
            "funding_rate":     client.get_funding_rate(asset),          # positiv = Longs zahlen (überhitzt long)
        }

        # H-012: OR Mid Shift (Value Area Bias) – aus breakout context hinzufügen
        if context and "or_mid_shift" in context:
            market_structure["or_bias"] = context.get("or_bias")
            market_structure["or_mid_shift"] = context["or_mid_shift"]

        oi_delta_str = f" Δ{oi_delta_pct:+.2f}%" if oi_delta_pct is not None else ""
        or_bias_str = f" | OR-Bias={context.get('or_bias', 'n/a')} Aligned={context.get('or_mid_shift', {}).get('bias_aligned', 'n/a')}" if context and "or_mid_shift" in context else ""
        print(f"   📊 Market-Structure: OI={market_structure['open_interest']:.0f}{oi_delta_str} | Long%={market_structure['long_account_pct']:.2%} | Taker={market_structure['taker_buy_ratio']:.3f} | Funding={market_structure['funding_rate']:+.4%}{or_bias_str}")
    except Exception as e:
        print(f"   ⚠️  Market-Structure Fetch fehlgeschlagen: {e}")

    # Fear & Greed Index (alternative.me, kostenlos, Tageswert).
    # Nur geloggt – kein Filter. Datenbasis für spätere Korrelationsanalyse.
    fear_greed = _fetch_fear_and_greed()
    if fear_greed:
        print(f"   😱 Fear & Greed: {fear_greed['value']} ({fear_greed['label']})")

    # Slippage: wie weit lag der Fill über (long) / unter (short) dem Breakout-Trigger?
    # Positive Werte = schlechter als Trigger (wir haben teurer gekauft / billiger verkauft).
    if direction == "long":
        slippage_usd = round(actual_entry - trigger_price, 6)
    else:
        slippage_usd = round(trigger_price - actual_entry, 6)

    # SL mit tatsächlichem Entry neu berechnen
    risk_actual = abs(actual_entry - stop_loss)

    # TP1: statisch bei 1:1 (halbe Size) – gesicherter Teilgewinn
    # TP2: statisch bei 3:1 (andere Hälfte) – maximiert Upside ohne Bitget 1%-Trailing-Clamp
    # Siehe H-002 im hypothesis_log: natives moving_plan ist bei Andres Kapital strukturell wertlos,
    # weil rangeRate auf Bitget-Minimum 1% hochclampt wird → Trailing läge faktisch bei Entry.
    if direction == "long":
        take_profit_1 = actual_entry + risk_actual * 1.0  # 1:1 statisch
        take_profit_2 = actual_entry + risk_actual * 3.0  # 3:1 statisch
    else:
        take_profit_1 = actual_entry - risk_actual * 1.0
        take_profit_2 = actual_entry - risk_actual * 3.0

    size_tp1 = round_size(asset, size / 2)
    size_tp2 = round_size(asset, size - size_tp1)

    # MIN_TRADE_SIZE-Check: Wenn TP1-Hälfte unter Bitget-Mindestgröße rundet,
    # gesamten Size auf TP2 routen statt eine TP1-Order mit 0 zu platzieren.
    min_sz = MIN_TRADE_SIZE.get(asset, 0.01)
    if size_tp1 < min_sz:
        print(f"   ℹ️  TP1-Size {size_tp1} < Min {min_sz} ({asset}) – Split übersprungen, alles auf TP2")
        size_tp1 = 0.0
        size_tp2 = size

    # Warten bis Position in API sichtbar ist (Bitget braucht 3-5s)
    if not DRY_RUN:
        time.sleep(5)

    # Prüfen ob Preset-SL vom Market-Order bereits aktiv ist
    existing_tpsl = client.get_tpsl_orders(asset)
    sl_ok = any(o.get("planType") == "loss_plan" for o in existing_tpsl)

    if sl_ok:
        print(f"   ✅ SL aktiv (Preset)")
    else:
        sl_r = client.place_stop_loss(asset, stop_loss, size, hold_side=hold_side)
        if not sl_r.success:
            time.sleep(2)
            sl_r = client.place_stop_loss(asset, stop_loss, size, hold_side=hold_side)
        sl_ok = sl_r.success
        print(f"   {'✅' if sl_ok else '❌'} SL {'gesetzt' if sl_ok else 'FEHLER: ' + str(sl_r.error)}")

    # Split Take-Profit platzieren (TP1 nur wenn Size > 0 — sonst TP2-only)
    if size_tp1 > 0:
        tp1_r = client.place_take_profit(asset, take_profit_1, size_tp1, hold_side=hold_side)
        if not tp1_r.success:
            time.sleep(2)
            tp1_r = client.place_take_profit(asset, take_profit_1, size_tp1, hold_side=hold_side)
        tp1_ok = tp1_r.success
        print(f"   {'✅' if tp1_ok else '❌'} TP1 1:1 @ ${take_profit_1:,.4f} (Size {size_tp1}) {'gesetzt' if tp1_ok else 'FEHLER: ' + str(tp1_r.error)}")
    else:
        tp1_ok = True  # bewusst übersprungen – kein Fehler
        print(f"   ⏭️  TP1 übersprungen (TP2-only Mode)")

    tp2_r = client.place_take_profit(asset, take_profit_2, size_tp2, hold_side=hold_side)
    if not tp2_r.success:
        time.sleep(2)
        tp2_r = client.place_take_profit(asset, take_profit_2, size_tp2, hold_side=hold_side)
    tp2_ok = tp2_r.success
    print(f"   {'✅' if tp2_ok else '❌'} TP2 3:1 @ ${take_profit_2:,.4f} (Size {size_tp2}) {'gesetzt' if tp2_ok else 'FEHLER: ' + str(tp2_r.error)}")

    tp_ok = tp1_ok and tp2_ok

    # KRITISCH: Kein SL = kein Schutz → sofort schließen (TP1 vorher canceln!)
    if not sl_ok:
        print(f"\n🚨 KRITISCH: SL nicht gesetzt! Cancle TP-Orders und schließe Position...")
        client.cancel_tpsl_orders(asset)
        close_result = client.place_market_order(
            coin=asset,
            is_buy=not is_buy,
            size=size,
            reduce_only=True,
        )
        close_ok = close_result.success
        alert_msg = (
            f"Problem beim {asset} {direction.upper()}{' [DRY]' if DRY_RUN else ''} — SL konnte nicht gesetzt werden. "
            f"{'Position wurde geschlossen.' if close_ok else 'Position ist noch offen — bitte sofort manuell schließen.'}"
        )
        send_telegram_message(alert_msg)
        return {"success": False, "error": "SL fehlgeschlagen – Position notgeschlossen"}

    # KRITISCH: TP2-Only-Mode (size_tp1=0) + TP2 fehlgeschlagen
    # → Trade hätte NUR SL als Exit, keinen Profit-Mechanismus → notschließen
    if size_tp1 == 0 and not tp2_ok:
        print(f"\n🚨 TP2-Only-Mode + TP2 FAIL → kein Profit-Mechanismus, notschließen...")
        # Reihenfolge wichtig: ERST close (reduce_only), DANN orphan-cancel.
        # Würden wir vorher cancel_tpsl_orders rufen, wäre auch der SL weg –
        # wenn dann place_market_order fehlschlägt, läuft die Position ungeschützt.
        close_result = client.place_market_order(
            coin=asset,
            is_buy=not is_buy,
            size=size,
            reduce_only=True,
        )
        if close_result.success:
            client.cancel_tpsl_orders(asset)  # SL+TP-Orphans aufräumen
            position_status = "geschlossen ✅"
        else:
            position_status = "KONNTE NICHT GESCHLOSSEN WERDEN ❌ – SL NOCH AKTIV – MANUELL HANDELN!"
        alert_msg = (
            f"Problem beim {asset} {direction.upper()}{' [DRY]' if DRY_RUN else ''} — kein Take-Profit setzbar. "
            f"{position_status}"
        )
        send_telegram_message(alert_msg)
        return {"success": False, "error": "TP2-Only + TP2 fail – notgeschlossen"}

    # TP unvollständig: SL ist aktiver Schutz → Warnung, Trade läuft weiter
    if not tp_ok:
        print(f"\n⚠️  TP unvollständig (TP1={tp1_ok}, TP2={tp2_ok}) – SL aktiv, Position läuft")
        alert_msg = (
            f"{asset} {direction.upper()}{' [DRY]' if DRY_RUN else ''} läuft, aber TP nicht vollständig gesetzt "
            f"(TP1 {'ok' if tp1_ok else 'fehlt'}, TP2 {'ok' if tp2_ok else 'fehlt'}). "
            f"SL ist aktiv bei ${stop_loss:,.4f}."
        )
        send_telegram_message(alert_msg)

    # Trade loggen (SL ist gesetzt; TP kann partiell fehlen aber Trade läuft)
    ctx = context or {}
    log_trade({
        # Entry
        "asset": asset,
        "direction": direction,
        "entry_price": actual_entry,
        "size": size,
        "leverage": LEVERAGE,
        "session": get_current_session(),
        # Exit-Planung
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,  # statisch bei 3:1 (siehe H-002)
        "size_tp1": size_tp1,
        "size_tp2": size_tp2,
        # Risiko
        "risk_usd": effective_risk,
        "risk_per_unit": round(abs(actual_entry - stop_loss), 6),
        "ratio": "Split 1:1 + 3:1",
        # Slippage (Fill vs. erwarteter Breakout-Trigger)
        "trigger_price": round(trigger_price, 6),
        "slippage_usd": slippage_usd,
        # Box-Kontext
        "box_high": ctx.get("box_high", box_high),
        "box_low": ctx.get("box_low", box_low),
        "box_range": ctx.get("box_range", box_high - box_low),
        "box_age_min": ctx.get("box_age_min"),
        "breakout_distance": ctx.get("breakout_distance"),
        # H-007: Late-Entry Trigger-Distance Ratio (Information-Logging, kein Filter)
        "trigger_distance_ratio": round(((ctx.get("breakout_distance", 0) - BREAKOUT_THRESHOLD.get(asset, entry_price * 0.002)) / ctx.get("box_range", 1)), 3) if ctx.get("box_range", 0) > 0 else None,
        # Volume-Kontext (für spätere Analyse)
        "volume_at_breakout": ctx.get("volume_at_breakout"),
        "volume_avg_20": ctx.get("volume_avg_20"),
        "volume_ratio": ctx.get("volume_ratio"),
        # Candle-Quality (professioneller ORB-Standard)
        "body_ratio": ctx.get("body_ratio"),          # |close-open|/range, <0.3 = Doji
        "close_position": ctx.get("close_position"),  # 1.0=Close@High, 0.0=Close@Low
        "scan_latency_sec": ctx.get("scan_latency_sec"),
        # Trend-Kontext (EMA-200/50, ATR-14, trend_direction, atr_ratio)
        "trend_context": ctx.get("trend_context", {}),
        # Market-Structure beim Entry (OI + Delta, Long/Short Ratio, Taker-Buy-Ratio, Funding)
        "market_structure": market_structure,
        # Fear & Greed Index (alternative.me, Tageswert, nur Logging)
        "fear_greed": fear_greed,
        # H-015: Regime-Snapshot (Klasse, Modifier, Begründung) — scharfes Sizing
        "regime_snapshot": regime_snapshot,
        # Meta
        "dry_run": DRY_RUN,
    })

    # H-011: ATR-Trail Shadow-Log (was würde der Trail-Stop tun wenn er bei 1R aktiviert?)
    log_h011_shadow(asset, direction, actual_entry, stop_loss, ctx.get("trend_context", {}))

    return {
        "success": True,
        "asset": asset,
        "direction": direction,
        "entry": actual_entry,
        "size": size,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "size_tp1": size_tp1,
        "size_tp2": size_tp2,
        "risk_usd": effective_risk,
        "sl_placed": sl_ok,
        "tp_placed": tp_ok,
    }


def update_and_get_hwm(balance: float) -> float:
    """Lädt High-Water-Mark, aktualisiert sie wenn Balance höher, gibt sie zurück."""
    hwm = CAPITAL
    if os.path.exists(HWM_FILE):
        try:
            with open(HWM_FILE) as f:
                hwm = json.load(f).get("hwm", CAPITAL)
        except (json.JSONDecodeError, OSError):
            pass
    if balance > hwm:
        hwm = balance
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_file = HWM_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump({"hwm": hwm, "updated": datetime.now().isoformat()}, f)
        os.replace(tmp_file, HWM_FILE)
    return hwm


def get_risk_usd(client):
    """Hole live Balance, berechne Max-Risk (inkl. Regime-Modifier H-015).

    Returns: (risk_usd, balance, regime_snapshot)
      - regime_snapshot: dict mit regime/risk_modifier/go/reason oder None wenn disabled
    """
    balance = client.get_balance()
    base_capital = balance if balance and balance > 0 else CAPITAL
    risk_pct_effective = MAX_RISK_PCT
    regime_snapshot = None
    if H015_REGIME_RISK_MODIFIER_ENABLED:
        try:
            from regime_detector import detect
            regime_snapshot = detect(use_cache=True)
            mod = float(regime_snapshot.get("risk_modifier", 1.0))
            risk_pct_effective = MAX_RISK_PCT * mod
        except Exception as e:
            print(f"   ⚠️  Regime-Detect fehlgeschlagen: {e} — voller Risk-Basiswert")
            regime_snapshot = {"regime": "error", "risk_modifier": 1.0,
                               "go": True, "error": str(e)}
    # H-016: Graduated Daily DD — Stufe 1 (daily_r <= HALF → Risk × 0.5)
    # Stufe 2 (daily_r <= KILL) wird separat im Main-Flow abgefangen (bestehende Logik).
    daily_r = load_daily_pnl().get("realized_r", 0.0)
    dd_half_applied = False
    if daily_r <= DAILY_DD_HALF_R:
        risk_pct_effective *= 0.5
        dd_half_applied = True
        print(f"   ⚠️  Graduated-DD Stufe 1: {daily_r:.2f}R heute → Risk halbiert")
    if regime_snapshot is not None:
        regime_snapshot["dd_half_applied"] = dd_half_applied
        regime_snapshot["daily_r"] = round(daily_r, 2)
    return base_capital * risk_pct_effective, base_capital, regime_snapshot


def scan_for_breakouts(client):
    """
    Scanne alle Assets auf Breakouts.
    Skipped Assets mit offenen Positionen, zu alten Boxen oder zu kleiner Range.
    Returns: (breakout dict or None, list of open Positions)
    """
    boxes = load_boxes()
    if not boxes:
        return None, []

    positions = client.get_positions()
    position_assets = [p.coin for p in positions]
    now = datetime.now()

    # Timing-Instrumentation: Beweise dass Scans auf 5m-Candle-Close aligned sind
    scan_ts = now.strftime("%H:%M:%S")
    expected_candle_close = now.replace(second=0, microsecond=0)
    # Letzte 5m-Candle-Close: abrunden auf volle 5 Minuten
    candle_close_min = (expected_candle_close.minute // 5) * 5
    expected_candle_close = expected_candle_close.replace(minute=candle_close_min)
    latency_sec = (now - expected_candle_close).total_seconds()
    print(f"   ⏱️  Scan {scan_ts} | Candle-Close {expected_candle_close.strftime('%H:%M')} | Latenz {latency_sec:.1f}s")

    current_session = get_current_session()

    for asset in ASSET_PRIORITY:
        if asset in position_assets:
            print(f"   ⏭️  {asset}: übersprungen (Position bereits offen)")
            log_skip("position_open", asset, current_session)
            continue
        if asset not in boxes:
            continue

        box = boxes[asset]

        # Alters-Check: Box darf maximal MAX_BOX_AGE_MIN alt sein
        try:
            box_ts = datetime.fromisoformat(box["timestamp"])
            age_min = (now - box_ts).total_seconds() / 60
            if age_min > MAX_BOX_AGE_MIN:
                print(f"   ⏭️  {asset}: Box {age_min:.0f} Min alt (Max {MAX_BOX_AGE_MIN} Min) – übersprungen")
                log_skip("box_too_old", asset, current_session, {
                    "age_min": round(age_min, 1),
                    "max_age_min": MAX_BOX_AGE_MIN,
                })
                continue
        except (KeyError, ValueError):
            print(f"   ⏭️  {asset}: Box ohne Timestamp – übersprungen (Sicherheit)")
            log_skip("box_missing_ts", asset, current_session)
            continue

        # Range-Check: Box muss Mindestbreite haben
        box_range = box["high"] - box["low"]
        min_range = MIN_BOX_RANGE.get(asset, box["high"] * 0.0005)
        if box_range < min_range:
            print(f"   ⏭️  {asset}: Box Range ${box_range:.4f} zu klein (Min ${min_range:.4f}) – übersprungen")
            log_skip("box_too_small", asset, current_session, {
                "box_range": round(box_range, 6),
                "min_range": round(min_range, 6),
            })
            continue

        try:
            current_price = client.get_price(asset)
        except Exception as e:
            print(f"   ⚠️  {asset}: Preisabfrage fehlgeschlagen ({e}) – übersprungen")
            log_skip("price_fetch_fail", asset, current_session, {"error": str(e)[:200]})
            continue
        direction = check_breakout(asset, current_price, box["high"], box["low"])

        if not direction:
            log_skip("no_breakout", asset, current_session, {
                "price": current_price,
                "box_high": box["high"],
                "box_low": box["low"],
                "box_range": round(box_range, 6),
            })
            continue

        # Late-Entry-Guard: Preis darf maximal 2x Box-Range über/unter Breakout-Level liegen
        breakout_dist = abs(current_price - (box["high"] if direction == "long" else box["low"]))
        max_dist = box_range * MAX_BREAKOUT_DISTANCE_RATIO
        if breakout_dist > max_dist:
            print(f"   ⏭️  {asset}: Breakout zu spät (${breakout_dist:.4f} > {MAX_BREAKOUT_DISTANCE_RATIO}x Range=${max_dist:.4f}) – übersprungen")
            log_skip("late_entry", asset, current_session, {
                "direction": direction,
                "breakout_dist": round(breakout_dist, 6),
                "box_range": round(box_range, 6),
                "ratio": round(breakout_dist / box_range, 3) if box_range else None,
            })
            continue

        # Candle-Close Confirmation + Volume-Daten für Logging
        # 21 Candles: 20 für Volume-Durchschnitt + 1 aktuelle (nicht abgeschlossen)
        volume_at_breakout = 0.0
        volume_avg_20 = 0.0
        volume_ratio = 0.0
        try:
            candles_5m = client.get_candles(asset, interval="5m", limit=21)
            if candles_5m and len(candles_5m) >= 2:
                last_closed = candles_5m[-2]
                candle_close = last_closed["close"]

                # Volume-Berechnung vor Candle-Check – damit auch candle_not_confirmed
                # Skips das Volume-Ratio im Skip-Log haben (für spätere Filterauswertung).
                volume_at_breakout = last_closed.get("volume", 0.0)
                past_volumes = [c["volume"] for c in candles_5m[:-2] if c.get("volume", 0) > 0]
                if past_volumes:
                    volume_avg_20 = sum(past_volumes) / len(past_volumes)
                    volume_ratio = volume_at_breakout / volume_avg_20 if volume_avg_20 > 0 else 0.0

                if direction == "long" and candle_close <= box["high"]:
                    print(f"   ⏭️  {asset}: Mid-Price ueber Box, aber 5m-Candle Close <= Box High -- Skip")
                    log_skip("candle_not_confirmed", asset, current_session, {
                        "direction": "long",
                        "candle_close": candle_close,
                        "box_high": box["high"],
                        "volume_ratio": round(volume_ratio, 3),
                    })
                    continue
                elif direction == "short" and candle_close >= box["low"]:
                    print(f"   ⏭️  {asset}: Mid-Price unter Box, aber 5m-Candle Close >= Box Low -- Skip")
                    log_skip("candle_not_confirmed", asset, current_session, {
                        "direction": "short",
                        "candle_close": candle_close,
                        "box_low": box["low"],
                        "volume_ratio": round(volume_ratio, 3),
                    })
                    continue

                # ── Candle-Body-Strength (professioneller ORB-Standard) ──
                # Institutionelle Regel: "Only take breakouts where the breakout candle
                # closes near its high (longs) or near its low (shorts)."
                # Doji/Spinning-Top (body < 30% der Range) = schwacher, unbestätigter Breakout.
                candle_open = last_closed.get("open", candle_close)
                candle_high_5m = last_closed.get("high", candle_close)
                candle_low_5m = last_closed.get("low", candle_close)
                candle_range_5m = candle_high_5m - candle_low_5m
                candle_body = abs(candle_close - candle_open)
                body_ratio = candle_body / candle_range_5m if candle_range_5m > 0 else 0.0

                # Close-Position: 1.0 = Close am High (bullisch), 0.0 = Close am Low (bearisch)
                close_position = (candle_close - candle_low_5m) / candle_range_5m if candle_range_5m > 0 else 0.5

                print(f"   🕯️  {asset}: Body {body_ratio:.0%} | Close-Pos {close_position:.0%} {'↑' if close_position > 0.5 else '↓'} | Vol {volume_ratio:.2f}x")

                if body_ratio < 0.3:
                    print(f"   ⏭️  {asset}: Schwache Breakout-Candle (Body {body_ratio:.0%} < 30%) – Doji/Spinning Top → Skip")
                    log_skip("weak_candle", asset, current_session, {
                        "direction": direction,
                        "body_ratio": round(body_ratio, 3),
                        "close_position": round(close_position, 3),
                        "candle_close": candle_close,
                        "volume_ratio": round(volume_ratio, 3),
                    })
                    continue

                # ── H-014 · Volume-Ratio-Filter ──
                # Fail-safe: Nur blockieren wenn volume_avg_20 > 0 (echte Daten vorliegen).
                if H014_VOLUME_FILTER_ENABLED and volume_avg_20 > 0 and volume_ratio < H014_VOLUME_RATIO_MIN:
                    print(f"   ⏭️  {asset}: Schwaches Volumen (Vol {volume_ratio:.2f}x < {H014_VOLUME_RATIO_MIN:.2f}x) → Skip")
                    log_skip("low_volume", asset, current_session, {
                        "direction": direction,
                        "volume_ratio": round(volume_ratio, 3),
                        "threshold": H014_VOLUME_RATIO_MIN,
                        "volume_at_breakout": round(volume_at_breakout, 3),
                        "volume_avg_20": round(volume_avg_20, 3),
                    })
                    continue
        except Exception as e:
            print(f"   ⚠️  {asset}: Candle-Check fehlgeschlagen ({e}) -- fahre fort")
            log_skip("api_error", asset, current_session, {"stage": "candle_check", "error": str(e)[:200]})

        box_range = box["high"] - box["low"]

        # Trend-Kontext: EMA-200, EMA-50, ATR-14 aus 15m-Candles berechnen.
        # 210 Kerzen = genug Warmup für EMA-200 (braucht min 200 Werte).
        # Nur geloggt, kein Filter – Datenbasis für spätere Hypothesen (H-001 Spur).
        trend_context = {}
        try:
            candles_15m = client.get_candles(asset, interval="15m", limit=210)
            if len(candles_15m) >= 200:
                closes = [c["close"] for c in candles_15m]

                # H-013: Volatility Squeeze (TTM) — Pure Python Implementation
                # Squeeze = Bollinger Bands komplett innerhalb Keltner Channels
                # → signalisiert Volatilitäts-Kontraktion + bevorstehenden explosiven Breakout
                is_squeezing = False
                try:
                    # Bollinger Bands: SMA(20) ± 2.0 * StDev(20)
                    sma_20 = _calc_sma(closes, 20)
                    stdev_20 = _calc_stdev(closes, 20)
                    upper_bb = sma_20 + (2.0 * stdev_20)
                    lower_bb = sma_20 - (2.0 * stdev_20)

                    # Keltner Channels: SMA(20) ± 1.5 * ATR(20)
                    atr_20 = _calc_atr(candles_15m, 20)
                    upper_kc = sma_20 + (1.5 * atr_20)
                    lower_kc = sma_20 - (1.5 * atr_20)

                    # Squeeze Condition: BB innerhalb KC
                    is_squeezing = (lower_bb > lower_kc) and (upper_bb < upper_kc)
                except Exception as e:
                    print(f"   ⚠️  {asset}: Squeeze-Berechnung fehlgeschlagen ({e})")

                ema_200 = _calc_ema(closes, 200)
                ema_50  = _calc_ema(closes, 50)
                atr_14  = _calc_atr(candles_15m, 14)
                trend_context = {
                    "ema_200": round(ema_200, 4),
                    "ema_50":  round(ema_50, 4),
                    "atr_14":  round(atr_14, 4),
                    "trend_direction": "above" if closes[-1] > ema_200 else "below",
                    "atr_ratio": round(box_range / atr_14, 3) if atr_14 > 0 else 0.0,
                    "is_squeezing": is_squeezing,  # H-013: Volatility Squeeze Flag
                }
            # 4H-Trend: EMA-50 auf 4-Stunden-Kerzen (≈25 Tage Lookback)
            # 150 Candles = 100 Warmup-Perioden für saubere EMA-50-Konvergenz.
            # (55 wäre nur 5 Warmup-Candles – zu wenig für stabile EMA.)
            try:
                candles_4h = client.get_candles(asset, interval="4H", limit=150)
                if len(candles_4h) >= 50:
                    closes_4h = [c["close"] for c in candles_4h]
                    ema_50_4h = _calc_ema(closes_4h, 50)
                    trend_context["h4_ema_50"]        = round(ema_50_4h, 4)
                    trend_context["h4_trend_direction"] = "above" if closes_4h[-1] > ema_50_4h else "below"
            except Exception:
                pass  # 4H optional – fehlt lieber als dass es den Trade blockiert
            if trend_context:
                # Alignment-Flags (nur Logging, kein Filter – Vorbereitung H-006)
                t15 = trend_context.get("trend_direction")
                if t15:
                    trend_context["ema_aligned"] = (
                        (direction == "long"  and t15 == "above") or
                        (direction == "short" and t15 == "below")
                    )
                t4h = trend_context.get("h4_trend_direction")
                if t4h:
                    trend_context["h4_aligned"] = (
                        (direction == "long"  and t4h == "above") or
                        (direction == "short" and t4h == "below")
                    )
                h4 = trend_context.get("h4_trend_direction", "?")
                a15 = "✅" if trend_context.get("ema_aligned") else "❌"
                a4h = "✅" if trend_context.get("h4_aligned") else "❌"
                print(f"   📐 Trend-Kontext: EMA200={trend_context.get('ema_200', 0):.4f} | ATR14={trend_context.get('atr_14', 0):.4f} | 15m={trend_context.get('trend_direction', '?')} {a15} | 4H={h4} {a4h} | Box/ATR={trend_context.get('atr_ratio', 0)}")
        except Exception as e:
            print(f"   ⚠️  {asset}: EMA/ATR-Berechnung fehlgeschlagen ({e})")

        # H-012: OR Mid Shift – VOR Filtern berechnen damit Shadow-Log auch geblockte Signale erfasst
        box_mid = (box["high"] + box["low"]) / 2.0
        prev_mid = box.get("prev_mid")  # Wird in save_opening_range.py berechnet
        or_bias = "neutral"
        bias_aligned = False
        if prev_mid is not None and prev_mid > 0:
            or_bias = "long" if box_mid > prev_mid else "short"
            bias_aligned = (direction == or_bias)

        # H-012 + H-013 Shadow-Log: jeden Breakout-Kandidaten erfassen (inkl. später gefilterter)
        log_h012_h013_shadow(asset, current_session, direction, current_price,
                              trend_context, or_bias, bias_aligned, box_mid, prev_mid)

        # H-006: EMA-200 Alignment Filter (kugelsicher)
        # Skip NUR wenn: Filter aktiv UND EMA-Daten valide UND Richtung gegen Trend.
        # Fail-safe: fehlende/invalide EMA-Daten → Trade läuft durch (wie vor dem Filter).
        if H006_EMA_FILTER_ENABLED:
            ema_200 = trend_context.get("ema_200") if trend_context else None
            ema_aligned = trend_context.get("ema_aligned") if trend_context else None
            # Nur blockieren wenn wir EINDEUTIG gegen den Trend handeln.
            # ema_aligned == False heißt: Daten waren da, Richtung passt nicht.
            if (ema_200 is not None and ema_200 > 0
                    and ema_aligned is False):
                print(f"   ⏭️  {asset}: {direction.upper()} gegen EMA-200 ({trend_context.get('trend_direction')}) – H-006 Skip")
                log_skip("ema200_misaligned", asset, current_session, {
                    "direction": direction,
                    "trend_direction": trend_context.get("trend_direction"),
                    "ema_200": ema_200,
                    "entry_price": current_price,
                })
                continue
            # Optional: zusätzlich 4H-Alignment fordern
            if H006_REQUIRE_H4_ALIGN:
                h4_aligned = trend_context.get("h4_aligned") if trend_context else None
                h4_ema = trend_context.get("h4_ema_50") if trend_context else None
                if (h4_ema is not None and h4_ema > 0
                        and h4_aligned is False):
                    print(f"   ⏭️  {asset}: {direction.upper()} gegen 4H-EMA-50 – H-006 Skip (4H)")
                    log_skip("ema200_h4_misaligned", asset, current_session, {
                        "direction": direction,
                        "h4_trend_direction": trend_context.get("h4_trend_direction"),
                        "h4_ema_50": h4_ema,
                        "entry_price": current_price,
                    })
                    continue

        return {
            "asset": asset,
            "direction": direction,
            "current_price": current_price,
            "box_high": box["high"],
            "box_low": box["low"],
            "box_range": box_range,
            "box_age_min": round(age_min, 1),
            "breakout_distance": abs(current_price - (box["high"] if direction == "long" else box["low"])),
            "volume_at_breakout": round(volume_at_breakout, 2),
            "volume_avg_20": round(volume_avg_20, 2),
            "volume_ratio": round(volume_ratio, 3),
            "body_ratio": round(body_ratio, 3),
            "close_position": round(close_position, 3),
            "scan_latency_sec": round(latency_sec, 1),
            "trend_context": trend_context,
            "or_bias": or_bias,
            "or_mid_shift": {"box_mid": round(box_mid, 6), "prev_mid": round(prev_mid, 6) if prev_mid else None, "bias_aligned": bias_aligned},
        }, positions

    return None, positions


def pre_trade_sanity_check(breakout: dict, risk_usd: float, balance: float) -> tuple:
    """Opt 1 – Blockiert Trades bei implausiblen Grunddaten.

    Checks:
      - Balance >= MIN_BALANCE_USD (sonst Min-Order nicht erreichbar)
      - entry_price > 0 und box_high > box_low (Box-Integrität)
      - SL-Abstand zum Entry plausibel (0 < dist < MAX_SL_DISTANCE_PCT * entry)
      - risk_usd > 0 und nicht absurd hoch

    Returns: (ok: bool, reason: str, context: dict)
    """
    ctx = {
        "balance": round(balance, 2),
        "risk_usd": round(risk_usd, 4),
        "asset": breakout.get("asset"),
        "direction": breakout.get("direction"),
    }
    if balance < MIN_BALANCE_USD:
        return False, "balance_too_low", {**ctx, "min_required": MIN_BALANCE_USD}

    entry = float(breakout.get("current_price", 0) or 0)
    box_high = float(breakout.get("box_high", 0) or 0)
    box_low = float(breakout.get("box_low", 0) or 0)
    if entry <= 0:
        return False, "invalid_entry_price", {**ctx, "entry_price": entry}
    if box_high <= box_low:
        return False, "invalid_box", {**ctx, "box_high": box_high, "box_low": box_low}

    direction = breakout.get("direction")
    box_range = box_high - box_low
    sl_buffer = max(box_range * 0.1, entry * 0.001)
    projected_sl = (box_low - sl_buffer) if direction == "long" else (box_high + sl_buffer)
    sl_distance = abs(entry - projected_sl)
    sl_distance_pct = sl_distance / entry if entry else 0

    if sl_distance <= 0:
        return False, "sl_distance_zero", {**ctx, "projected_sl": projected_sl, "entry": entry}
    if sl_distance_pct > MAX_SL_DISTANCE_PCT:
        return False, "sl_distance_too_wide", {
            **ctx, "sl_distance_pct": round(sl_distance_pct, 4),
            "max_allowed_pct": MAX_SL_DISTANCE_PCT,
        }

    if risk_usd <= 0:
        return False, "risk_usd_invalid", {**ctx}
    # Absurditätsschutz: > 5x MAX_RISK_USD deutet auf Config/Balance-Bug hin
    if risk_usd > MAX_RISK_USD * 5:
        return False, "risk_usd_excessive", {**ctx, "max_expected": MAX_RISK_USD * 5}

    return True, "ok", {**ctx, "sl_distance_pct": round(sl_distance_pct, 4)}


# ---------------------------------------------------------------------------
# Daily Drawdown Tracker (Opt 2)
# ---------------------------------------------------------------------------
DAILY_PNL_FILE = os.path.join(DATA_DIR, "daily_pnl.json")


def load_daily_pnl() -> dict:
    """Lädt Tages-PnL-Tracker. Auto-Reset bei Datumswechsel."""
    today = datetime.now().strftime("%Y-%m-%d")
    default = {"date": today, "realized_pnl_usd": 0.0, "realized_r": 0.0,
               "trades_closed": 0, "kill_alert_sent": False}
    if not os.path.exists(DAILY_PNL_FILE):
        return default
    try:
        with open(DAILY_PNL_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") != today:
            return default  # Neuer Tag → Reset
        return {**default, **data}
    except (json.JSONDecodeError, OSError):
        return default


def save_daily_pnl(data: dict) -> None:
    """Atomar speichern."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DAILY_PNL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DAILY_PNL_FILE)


def check_daily_dd_breaker() -> tuple:
    """Opt 2 – Tages-DD-Circuit-Breaker.

    Returns: (ok: bool, reason: str, context: dict)
    """
    d = load_daily_pnl()
    daily_r = d.get("realized_r", 0.0)
    if daily_r <= DAILY_DD_KILL_R:
        return False, "daily_dd_kill", {
            "date": d["date"],
            "daily_r": round(daily_r, 2),
            "daily_pnl_usd": round(d.get("realized_pnl_usd", 0.0), 2),
            "trades_closed": d.get("trades_closed", 0),
            "threshold_r": DAILY_DD_KILL_R,
            "alert_sent": d.get("kill_alert_sent", False),
        }
    return True, "ok", {"daily_r": round(daily_r, 2)}


def mark_daily_dd_alert_sent() -> None:
    """Verhindert wiederholte Telegram-Alerts beim selben Tages-Kill."""
    d = load_daily_pnl()
    d["kill_alert_sent"] = True
    save_daily_pnl(d)


def main(scan_only: bool = False):
    """Main Trade-Entry. Mit scan_only=True werden alle Checks gelaufen, aber
    KEIN Trade ausgeführt und KEINE Telegram-Nachricht gesendet. Für Canary-Runs
    nach Deploy oder manuelle Verifikation während der Session.
    """
    print("=" * 60)
    mode_tag = " [SCAN-ONLY]" if scan_only else ""
    print(f"APEX - Autonomous Trade Check{mode_tag}")
    print("=" * 60)

    # File-Lock: verhindert parallele Ausführung durch Cron-Überlappung
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("⏳ Anderer autonomous_trade.py läuft noch – Abbruch.")
        lock_fd.close()
        return

    try:
        if DRY_RUN:
            print("⚠️  DRY RUN MODUS - kein echtes Geld")

        # Session prüfen (im Scan-Only-Modus tolerant: erlaubt Tests außerhalb Session)
        session = get_current_session()
        if not session:
            if scan_only:
                print("ℹ️  Außerhalb der Trading-Sessions – Scan-Only läuft trotzdem weiter")
                session = "scan-only"
            else:
                print("⚠️  Außerhalb der Trading-Sessions")
                log_skip("no_session", None, None, {"hour": datetime.now().hour})
                print("NO_REPLY")
                return

        print(f"📍 Session: {session.upper()}")

        # Bereits getradet heute?
        if has_traded_today_in_session(session):
            msg = f"⏭️ APEX {session.upper()}: Skip – bereits getradet"
            print(f"\n✅ {msg}")
            log_skip("already_traded", None, session)
            print("NO_REPLY")
            return

        # Opt 2: Daily Drawdown Circuit Breaker – vor API-Calls prüfen
        dd_ok, dd_reason, dd_ctx = check_daily_dd_breaker()
        if not dd_ok:
            print(f"\n🛑 DAILY-DD-KILL: {dd_ctx['daily_r']}R heute (Limit {dd_ctx['threshold_r']}R)")
            log_skip(dd_reason, None, session, dd_ctx)
            if not dd_ctx.get("alert_sent"):
                send_telegram_message(
                    f"Tages-Limit erreicht: {dd_ctx['daily_r']}R "
                    f"(${dd_ctx['daily_pnl_usd']:+.2f}) nach {dd_ctx['trades_closed']} Trades heute. "
                    f"Keine neuen Trades bis morgen."
                )
                mark_daily_dd_alert_sent()
            print("NO_REPLY")
            return

        client = BitgetClient(dry_run=DRY_RUN)

        # Breakout suchen (scan_for_breakouts holt intern get_positions und gibt sie zurück)
        print("\n🔍 Suche Breakouts...")
        breakout, positions = scan_for_breakouts(client)

        # Offene Positionen aus dem Scan-Ergebnis ableiten (kein extra API-Call)
        if not breakout and positions:
            pos_list = ", ".join([f"{p.coin} {'LONG' if p.size > 0 else 'SHORT'}" for p in positions])
            print(f"\n📊 Bestehende Positionen: {pos_list}")

        if not breakout:
            msg = f"🔍 APEX {session.upper()}: Kein Breakout – kein Trade"
            print(f"   {msg}")
            print("NO_REPLY")
            return

        print(f"\n🎯 BREAKOUT!")
        print(f"   {breakout['asset']} {breakout['direction'].upper()}")
        print(f"   Preis: ${breakout['current_price']:,.4f}")
        print(f"   Box:   ${breakout['box_low']:,.4f} – ${breakout['box_high']:,.4f}")
        print(f"   Distanz: ${breakout['breakout_distance']:,.4f}")

        # Live Balance für Risk-Berechnung holen (inkl. Regime-Modifier H-015)
        risk_usd, balance, regime_snapshot = get_risk_usd(client)
        if regime_snapshot:
            print(f"\n🌡️  Regime: {regime_snapshot.get('regime')} "
                  f"(mod {regime_snapshot.get('risk_modifier', 1.0):.2f}) — "
                  f"{regime_snapshot.get('reason', '')}")
        print(f"💰 Balance: ${balance:.2f} USDT | Risk/Trade: ${risk_usd:.2f}")

        # H-015: Regime-NO-TRADE-Gate (crash / risk_modifier=0)
        if regime_snapshot and not regime_snapshot.get("go", True):
            msg = (f"Regime-Gate greift: {regime_snapshot.get('regime')}. "
                   f"{regime_snapshot.get('reason', '')}. Keine neuen Trades heute.")
            print(f"\n🛑 REGIME NO-TRADE: {regime_snapshot.get('regime')}")
            log_skip("regime_no_trade", breakout["asset"], session, {
                "regime": regime_snapshot.get("regime"),
                "risk_modifier": regime_snapshot.get("risk_modifier"),
                "reason": regime_snapshot.get("reason"),
            })
            if not scan_only:
                send_telegram_message(msg)
            print("NO_REPLY")
            return

        # Kill-Switch: 50% Drawdown vom High-Water-Mark → keine neuen Trades
        hwm = update_and_get_hwm(balance)
        kill_threshold = hwm * (1 - DRAWDOWN_KILL_PCT)
        if balance < kill_threshold and not DRY_RUN:
            msg = (
                f"Wir stoppen heute. Book bei ${balance:.2f}, das ist mehr als "
                f"{int(DRAWDOWN_KILL_PCT*100)}% unter dem Hochpunkt (${hwm:.2f}). "
                f"Keine neuen Trades bis zur manuellen Freigabe."
            )
            print(f"\n🛑 KILL-SWITCH: Balance ${balance:.2f} < ${kill_threshold:.2f} (HWM ${hwm:.2f}) – Stop!")
            log_skip("kill_switch", breakout["asset"], session, {
                "balance": round(balance, 2),
                "hwm": round(hwm, 2),
                "kill_threshold": round(kill_threshold, 2),
            })
            send_telegram_message(msg)
            print("NO_REPLY")
            return

        # Opt 1: Pre-Trade Sanity Check
        sanity_ok, sanity_reason, sanity_ctx = pre_trade_sanity_check(breakout, risk_usd, balance)
        if not sanity_ok:
            print(f"\n🛑 SANITY CHECK FAIL: {sanity_reason}")
            print(f"   Context: {sanity_ctx}")
            log_skip(sanity_reason, breakout["asset"], session, sanity_ctx)
            if not scan_only:
                send_telegram_message(
                    f"Trade blockiert ({breakout['asset']} {breakout['direction'].upper()}): "
                    f"{sanity_reason}. Kontext: {sanity_ctx}"
                )
            print("NO_REPLY")
            return
        print(f"\n✅ Sanity Check OK: SL-Distanz {sanity_ctx.get('sl_distance_pct', 0)*100:.2f}%")

        # Scan-Only: Alle Checks gelaufen, aber kein Trade. Ausstieg hier.
        if scan_only:
            print("\n🔎 SCAN-ONLY: Echter Trade würde JETZT ausgelöst werden.")
            print(f"   Asset: {breakout['asset']} | Direction: {breakout['direction'].upper()}")
            print(f"   Entry: ${breakout['current_price']:,.4f} | Risk: ${risk_usd:.2f}")
            print("   → Keine Order platziert, kein Telegram gesendet.")
            print("NO_REPLY")
            return {"success": True, "scan_only": True, "would_trade": breakout}

        # Trade ausführen
        print(f"\n🚀 Führe {breakout['direction']} Trade aus...")
        result = execute_breakout_trade(
            client,
            breakout["asset"],
            breakout["direction"],
            breakout["current_price"],
            breakout["box_high"],
            breakout["box_low"],
            risk_usd,
            context=breakout,
            regime_snapshot=regime_snapshot,
        )

        dry_tag = " [DRY]" if DRY_RUN else ""

        if result["success"]:
            print(f"\n✅ TRADE AUSGEFÜHRT{dry_tag}")
            print(f"   Entry:       ${result['entry']:,.4f}")
            print(f"   Size:        {result['size']}")
            print(f"   Stop-Loss:   ${result['stop_loss']:,.4f}  (Risk: ${result['risk_usd']:.2f})")
            print(f"   TP1 (1:1):   ${result['take_profit_1']:,.4f}  (Size {result['size_tp1']})")
            print(f"   TP2 (3:1):   ${result['take_profit_2']:,.4f}  (Size {result['size_tp2']})")
            print(f"   Hebel:       {LEVERAGE}x")

            direction_label = "Long" if result["direction"] == "long" else "Short"
            session_label = (get_current_session() or "?").upper()
            msg = (
                f"Wir sind in einem {result['asset']} {direction_label}{dry_tag}. "
                f"Entry bei ${result['entry']:,.4f}, SL bei ${result['stop_loss']:,.4f} "
                f"(Risiko −${result['risk_usd']:.2f}). "
                f"TP1 ${result['take_profit_1']:,.4f} · TP2 ${result['take_profit_2']:,.4f}. Let's go."
            )
            send_telegram_message(msg)
        else:
            print(f"\n❌ TRADE FEHLGESCHLAGEN: {result.get('error')}")
            send_telegram_message(f"Trade hat nicht geklappt{dry_tag} — {result.get('error')}")

        print("NO_REPLY")
        return result

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()
    scan_only_flag = "--scan-only" in sys.argv
    try:
        result = main(scan_only=scan_only_flag)
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"Bot-Fehler (autonomous_trade) — {e}")
        print("NO_REPLY")
        sys.exit(1)
