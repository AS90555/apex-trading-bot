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
        MIN_BOX_RANGE, MAX_BOX_AGE_MIN, MAX_BREAKOUT_DISTANCE_RATIO
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

MAX_RISK_USD = CAPITAL * MAX_RISK_PCT

# Datenpfade
BOXES_FILE = os.path.join(DATA_DIR, "opening_range_boxes.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
LOCK_FILE = os.path.join(DATA_DIR, "autonomous_trade.lock")
HWM_FILE = os.path.join(DATA_DIR, "high_water_mark.json")
SKIP_LOG_FILE = os.path.join(DATA_DIR, "skip_log.jsonl")


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


def execute_breakout_trade(client, asset, direction, entry_price, box_high, box_low, risk_usd=None, context=None):
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
        alert = f"⚠️ APEX: TP/SL Cancel für {asset} fehlgeschlagen – Trade abgebrochen"
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

    # Market-Structure beim Entry: OI, Long-Account-%, Taker-Buy-Ratio, Funding Rate.
    # Nur geloggt – kein Filter. Datenbasis für Spur 3 Analyse nach 30 Trades.
    market_structure = {}
    try:
        market_structure = {
            "open_interest":    client.get_open_interest(asset),
            "long_account_pct": client.get_long_account_ratio(asset),   # z.B. 0.68 = 68% Long-Accounts
            "taker_buy_ratio":  client.get_taker_ratio(asset),           # >0.55 bullish, <0.45 bearish
            "funding_rate":     client.get_funding_rate(asset),          # positiv = Longs zahlen (überhitzt long)
        }
        print(f"   📊 Market-Structure: OI={market_structure['open_interest']:.0f} | Long%={market_structure['long_account_pct']:.2%} | Taker={market_structure['taker_buy_ratio']:.3f} | Funding={market_structure['funding_rate']:+.4%}")
    except Exception as e:
        print(f"   ⚠️  Market-Structure Fetch fehlgeschlagen: {e}")

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
        alert_msg = (
            f"🚨 APEX NOTFALL-SCHLIESSUNG{' [DRY RUN]' if DRY_RUN else ''}\n\n"
            f"{asset} {direction.upper()} – SL konnte NICHT gesetzt werden!\n"
            f"Position {'notgeschlossen ✅' if close_result.success else 'KONNTE NICHT GESCHLOSSEN WERDEN ❌ – MANUELL HANDELN!'}"
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
            f"🚨 APEX NOTFALL-SCHLIESSUNG{' [DRY RUN]' if DRY_RUN else ''}\n\n"
            f"{asset} {direction.upper()} – TP2-Only-Mode + TP2 FEHLER\n"
            f"TP1 zu klein, TP2 @ ${take_profit_2:,.4f} nicht setzbar → kein Profit-Take möglich\n"
            f"Position {position_status}"
        )
        send_telegram_message(alert_msg)
        return {"success": False, "error": "TP2-Only + TP2 fail – notgeschlossen"}

    # TP unvollständig: SL ist aktiver Schutz → Warnung, Trade läuft weiter
    if not tp_ok:
        print(f"\n⚠️  TP unvollständig (TP1={tp1_ok}, TP2={tp2_ok}) – SL aktiv, Position läuft")
        alert_msg = (
            f"⚠️ APEX TP-Warnung{' [DRY RUN]' if DRY_RUN else ''}\n\n"
            f"{asset} {direction.upper()} – TP nicht vollständig gesetzt\n"
            f"TP1={'✅' if tp1_ok else '❌'} | TP2={'✅' if tp2_ok else '❌'}\n"
            f"SL aktiv @ ${stop_loss:,.4f} – Position läuft weiter."
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
        # Market-Structure beim Entry (OI, Long/Short Ratio, Taker-Buy-Ratio)
        "market_structure": market_structure,
        # Meta
        "dry_run": DRY_RUN,
    })

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
    """Hole live Balance und berechne Max-Risk in USD"""
    balance = client.get_balance()
    if balance and balance > 0:
        return balance * MAX_RISK_PCT, balance
    # Fallback auf Config-Wert falls API-Fehler
    return CAPITAL * MAX_RISK_PCT, CAPITAL


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
                ema_200 = _calc_ema(closes, 200)
                ema_50  = _calc_ema(closes, 50)
                atr_14  = _calc_atr(candles_15m, 14)
                trend_context = {
                    "ema_200": round(ema_200, 4),
                    "ema_50":  round(ema_50, 4),
                    "atr_14":  round(atr_14, 4),
                    "trend_direction": "above" if closes[-1] > ema_200 else "below",
                    "atr_ratio": round(box_range / atr_14, 3) if atr_14 > 0 else 0.0,
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
                h4 = trend_context.get("h4_trend_direction", "?")
                print(f"   📐 Trend-Kontext: EMA200={trend_context.get('ema_200', 0):.4f} | ATR14={trend_context.get('atr_14', 0):.4f} | 15m={trend_context.get('trend_direction', '?')} | 4H={h4} | Box/ATR={trend_context.get('atr_ratio', 0)}")
        except Exception as e:
            print(f"   ⚠️  {asset}: EMA/ATR-Berechnung fehlgeschlagen ({e})")

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
        }, positions

    return None, positions


def main():
    print("=" * 60)
    print("APEX - Autonomous Trade Check")
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

        # Session prüfen
        session = get_current_session()
        if not session:
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
            send_telegram_message(msg)
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
            send_telegram_message(msg)
            print("NO_REPLY")
            return

        print(f"\n🎯 BREAKOUT!")
        print(f"   {breakout['asset']} {breakout['direction'].upper()}")
        print(f"   Preis: ${breakout['current_price']:,.4f}")
        print(f"   Box:   ${breakout['box_low']:,.4f} – ${breakout['box_high']:,.4f}")
        print(f"   Distanz: ${breakout['breakout_distance']:,.4f}")

        # Live Balance für Risk-Berechnung holen
        risk_usd, balance = get_risk_usd(client)
        print(f"\n💰 Balance: ${balance:.2f} USDT | Risk/Trade: ${risk_usd:.2f}")

        # Kill-Switch: 50% Drawdown vom High-Water-Mark → keine neuen Trades
        hwm = update_and_get_hwm(balance)
        kill_threshold = hwm * (1 - DRAWDOWN_KILL_PCT)
        if balance < kill_threshold and not DRY_RUN:
            msg = (
                f"🛑 APEX KILL-SWITCH AKTIV\n\n"
                f"Balance ${balance:.2f} USDT – mehr als {int(DRAWDOWN_KILL_PCT*100)}% "
                f"unter High-Water-Mark (${hwm:.2f} USDT)\n"
                f"Schwelle: ${kill_threshold:.2f} USDT\n"
                f"Keine neuen Trades bis manuell freigegeben."
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
        )

        dry_tag = " [DRY RUN]" if DRY_RUN else ""

        if result["success"]:
            print(f"\n✅ TRADE AUSGEFÜHRT{dry_tag}")
            print(f"   Entry:       ${result['entry']:,.4f}")
            print(f"   Size:        {result['size']}")
            print(f"   Stop-Loss:   ${result['stop_loss']:,.4f}  (Risk: ${result['risk_usd']:.2f})")
            print(f"   TP1 (1:1):   ${result['take_profit_1']:,.4f}  (Size {result['size_tp1']})")
            print(f"   TP2 (3:1):   ${result['take_profit_2']:,.4f}  (Size {result['size_tp2']})")
            print(f"   Hebel:       {LEVERAGE}x")

            direction_emoji = "🟢" if result["direction"] == "long" else "🔴"
            msg = (
                f"🚀 APEX TRADE{dry_tag}\n\n"
                f"{direction_emoji} {result['asset']} {result['direction'].upper()}\n"
                f"Entry: ${result['entry']:,.4f}\n"
                f"Size: {result['size']}\n"
                f"Stop-Loss: ${result['stop_loss']:,.4f} (Risk: ${result['risk_usd']:.2f})\n"
                f"Exit-Strategie:\n"
                f"  TP1 (1:1): ${result['take_profit_1']:,.4f} (Size {result['size_tp1']})\n"
                f"  TP2 (3:1): ${result['take_profit_2']:,.4f} (Size {result['size_tp2']})\n"
                f"Hebel: {LEVERAGE}x | Split 1:1 + 3:1"
            )
            send_telegram_message(msg)
        else:
            print(f"\n❌ TRADE FEHLGESCHLAGEN: {result.get('error')}")
            send_telegram_message(f"❌ APEX TRADE FEHLER{dry_tag}: {result.get('error')}")

        print("NO_REPLY")
        return result

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()
    try:
        result = main()
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"💥 APEX autonomous_trade.py ERROR: {e}")
        print("NO_REPLY")
        sys.exit(1)
