#!/usr/bin/env python3
"""
APEX - Autonomous Trading Script
=================================
Wird von Cron Jobs aufgerufen, checkt Breakouts, platziert Orders autonom.
"""

import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient
from telegram_sender import send_telegram_message

# Config laden
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import (
        DRY_RUN, CAPITAL, MAX_RISK_PCT, ASSET_PRIORITY,
        BREAKOUT_THRESHOLD, LEVERAGE, SIZE_DECIMALS
    )
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0
    MAX_RISK_PCT = 0.02
    ASSET_PRIORITY = ["ETH", "SOL", "AVAX"]
    BREAKOUT_THRESHOLD = {"ETH": 5.0, "SOL": 0.30, "AVAX": 0.15}
    LEVERAGE = 5
    SIZE_DECIMALS = {"ETH": 2, "SOL": 1, "AVAX": 0}

MAX_RISK_USD = CAPITAL * MAX_RISK_PCT

# Datenpfade
BOXES_FILE = os.path.join(DATA_DIR, "opening_range_boxes.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")


def load_boxes():
    if not os.path.exists(BOXES_FILE):
        return {}
    with open(BOXES_FILE, "r") as f:
        return json.load(f)


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
    """Prüfe ob in dieser Session heute schon getradet wurde"""
    if not os.path.exists(TRADES_FILE):
        return False

    with open(TRADES_FILE, "r") as f:
        trades = json.load(f)

    today = datetime.now().date().isoformat()
    for trade in trades:
        trade_date = trade.get("timestamp", "")[:10]
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
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)

    trades.append({
        **trade_data,
        "timestamp": datetime.now().isoformat(),
        "session": get_current_session(),
    })

    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


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


def execute_breakout_trade(client, asset, direction, entry_price, box_high, box_low, risk_usd=None):
    """
    Platziere Breakout Trade mit Stop-Loss und Take-Profit.
    Returns: dict mit Trade-Ergebnis
    """
    # Stop-Loss: andere Seite der Box + kleiner Puffer
    puffer = BREAKOUT_THRESHOLD.get(asset, entry_price * 0.002)
    if direction == "long":
        stop_loss = box_low - puffer
    else:
        stop_loss = box_high + puffer

    # Risk: live Balance verwenden falls übergeben, sonst Fallback
    effective_risk = risk_usd if risk_usd is not None else MAX_RISK_USD

    # Position Size: Risk / Stop-Distanz
    size = client.calculate_position_size(effective_risk, entry_price, stop_loss)
    size = round_size(asset, size)

    if size <= 0:
        return {"success": False, "error": "Berechnete Size zu klein"}

    # Leverage setzen
    client.set_leverage(asset, LEVERAGE)

    # Take-Profit: 2:1 Risk/Reward
    risk_per_coin = abs(entry_price - stop_loss)
    reward_per_coin = risk_per_coin * 2
    if direction == "long":
        take_profit = entry_price + reward_per_coin
    else:
        take_profit = entry_price - reward_per_coin

    is_buy = (direction == "long")

    # Market Order mit integriertem SL/TP
    order_result = client.place_market_order(
        coin=asset,
        is_buy=is_buy,
        size=size,
        reduce_only=False,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )

    if not order_result.success:
        return {"success": False, "error": order_result.error}

    actual_entry = order_result.avg_price

    # SL/TP mit tatsächlichem Entry neu berechnen und separat setzen
    # (Preset im Market-Order-Call reicht oft aus, aber sicherheitshalber)
    risk_actual = abs(actual_entry - stop_loss)
    if direction == "long":
        stop_loss = actual_entry - risk_actual
        take_profit = actual_entry + risk_actual * 2
    else:
        stop_loss = actual_entry + risk_actual
        take_profit = actual_entry - risk_actual * 2

    sl_result = client.place_stop_loss(asset, stop_loss, size)
    tp_result = client.place_take_profit(asset, take_profit, size)

    # Trade loggen
    log_trade({
        "asset": asset,
        "direction": direction,
        "entry_price": actual_entry,
        "size": size,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_usd": effective_risk,
        "reward_usd": effective_risk * 2,
        "ratio": "2:1",
        "leverage": LEVERAGE,
        "dry_run": DRY_RUN,
    })

    return {
        "success": True,
        "asset": asset,
        "direction": direction,
        "entry": actual_entry,
        "size": size,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_usd": effective_risk,
        "sl_placed": sl_result.success,
        "tp_placed": tp_result.success,
    }


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
    Skipped Assets mit offenen Positionen.
    Returns: dict oder None
    """
    boxes = load_boxes()
    if not boxes:
        return None

    positions = client.get_positions()
    position_assets = [p.coin for p in positions]

    for asset in ASSET_PRIORITY:
        if asset in position_assets:
            print(f"   ⏭️  {asset}: übersprungen (Position bereits offen)")
            continue
        if asset not in boxes:
            continue

        box = boxes[asset]
        current_price = client.get_price(asset)
        direction = check_breakout(asset, current_price, box["high"], box["low"])

        if direction:
            return {
                "asset": asset,
                "direction": direction,
                "current_price": current_price,
                "box_high": box["high"],
                "box_low": box["low"],
                "breakout_size": abs(current_price - (box["high"] if direction == "long" else box["low"])),
            }

    return None


def main():
    print("=" * 60)
    print("APEX - Autonomous Trade Check")
    print("=" * 60)

    if DRY_RUN:
        print("⚠️  DRY RUN MODUS - kein echtes Geld")

    # Session prüfen
    session = get_current_session()
    if not session:
        print("⚠️  Außerhalb der Trading-Sessions")
        print("NO_REPLY")
        return

    print(f"📍 Session: {session.upper()}")

    # Bereits getradet heute?
    if has_traded_today_in_session(session):
        msg = f"⏭️ APEX {session.upper()}: Skip – bereits getradet"
        print(f"\n✅ {msg}")
        send_telegram_message(msg)
        print("NO_REPLY")
        return

    client = BitgetClient(dry_run=DRY_RUN)

    # Offene Positionen
    positions = client.get_positions()
    if positions:
        pos_list = ", ".join([f"{p.coin} {'LONG' if p.size > 0 else 'SHORT'}" for p in positions])
        print(f"\n📊 Bestehende Positionen: {pos_list}")

    # Breakout suchen
    print("\n🔍 Suche Breakouts...")
    breakout = scan_for_breakouts(client)

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
    print(f"   Distanz: ${breakout['breakout_size']:,.4f}")

    # Live Balance für Risk-Berechnung holen
    risk_usd, balance = get_risk_usd(client)
    print(f"\n💰 Balance: ${balance:.2f} USDT | Risk/Trade: ${risk_usd:.2f}")

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
    )

    dry_tag = " [DRY RUN]" if DRY_RUN else ""

    if result["success"]:
        print(f"\n✅ TRADE AUSGEFÜHRT{dry_tag}")
        print(f"   Entry:       ${result['entry']:,.4f}")
        print(f"   Size:        {result['size']}")
        print(f"   Stop-Loss:   ${result['stop_loss']:,.4f}  (Risk: ${result['risk_usd']:.2f})")
        print(f"   Take-Profit: ${result['take_profit']:,.4f}  (Reward: ${result['risk_usd'] * 2:.2f})")
        print(f"   Hebel:       {LEVERAGE}x")
        print(f"   SL: {'✅' if result['sl_placed'] else '❌'} | TP: {'✅' if result['tp_placed'] else '❌'}")

        direction_emoji = "🟢" if result["direction"] == "long" else "🔴"
        msg = (
            f"🚀 APEX TRADE{dry_tag}\n\n"
            f"{direction_emoji} {result['asset']} {result['direction'].upper()}\n"
            f"Entry: ${result['entry']:,.4f}\n"
            f"Size: {result['size']}\n"
            f"Stop-Loss: ${result['stop_loss']:,.4f} (Risk: ${result['risk_usd']:.2f})\n"
            f"Take-Profit: ${result['take_profit']:,.4f} (Reward: ${result['risk_usd'] * 2:.2f})\n"
            f"Hebel: {LEVERAGE}x | R:R 2:1\n"
            f"SL: {'✅' if result['sl_placed'] else '❌'} | TP: {'✅' if result['tp_placed'] else '❌'}"
        )
        send_telegram_message(msg)
    else:
        print(f"\n❌ TRADE FEHLGESCHLAGEN: {result.get('error')}")
        send_telegram_message(f"❌ APEX TRADE FEHLER{dry_tag}: {result.get('error')}")

    print("NO_REPLY")
    return result


if __name__ == "__main__":
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
