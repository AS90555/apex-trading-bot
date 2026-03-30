#!/usr/bin/env python3
"""
APEX - Session Summary Reporter
================================
Sendet Session-Zusammenfassungen an Telegram.
Wird von Final-Check Crons aufgerufen.
"""

import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient
from telegram_sender import send_telegram_message

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import (
        DRY_RUN, CAPITAL, MAX_RISK_PCT,
        ASSET_PRIORITY as ASSETS, BREAKOUT_THRESHOLD
    )
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0
    MAX_RISK_PCT = 0.02
    ASSETS = ["ETH", "SOL", "AVAX"]
    BREAKOUT_THRESHOLD = {"ETH": 5.0, "SOL": 0.30, "AVAX": 0.15}

BOXES_FILE = os.path.join(DATA_DIR, "opening_range_boxes.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")

SESSION_NAMES = {
    "tokyo": "Tokyo",
    "eu": "Europa",
    "us": "USA"
}

SESSION_EMOJIS = {
    "tokyo": "🌏",
    "eu": "🇪🇺",
    "us": "🇺🇸"
}


def load_boxes():
    if not os.path.exists(BOXES_FILE):
        return {}
    try:
        with open(BOXES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def has_traded_in_session(session):
    if not os.path.exists(TRADES_FILE):
        return False, None
    try:
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False, None

    today = datetime.now().date().isoformat()
    for trade in trades:
        trade_date = trade.get("timestamp", "")[:10]
        trade_session = trade.get("session", "")
        if trade_date == today and trade_session == session:
            return True, trade
    return False, None


def check_breakout(asset, price, box_high, box_low):
    threshold = BREAKOUT_THRESHOLD.get(asset, price * 0.002)
    if price > box_high + threshold:
        return "long"
    elif price < box_low - threshold:
        return "short"
    return None


def get_session_breakouts(client):
    boxes = load_boxes()
    if not boxes:
        return {}

    breakouts = {}
    for asset in ASSETS:
        if asset not in boxes:
            breakouts[asset] = {"status": "no_box", "direction": None}
            continue

        box = boxes[asset]
        try:
            current_price = client.get_price(asset)
        except Exception:
            breakouts[asset] = {"status": "error", "direction": None}
            continue

        direction = check_breakout(asset, current_price, box["high"], box["low"])
        if direction:
            breakout_size = abs(current_price - (box["high"] if direction == "long" else box["low"]))
            breakouts[asset] = {
                "status": "breakout",
                "direction": direction,
                "price": current_price,
                "box_high": box["high"],
                "box_low": box["low"],
                "breakout_size": breakout_size,
            }
        else:
            breakouts[asset] = {
                "status": "no_breakout",
                "direction": None,
                "price": current_price,
            }
    return breakouts


def format_summary(session):
    emoji = SESSION_EMOJIS.get(session, "📊")
    name = SESSION_NAMES.get(session, session.upper())
    dry_tag = " [DRY RUN]" if DRY_RUN else ""

    client = BitgetClient(dry_run=DRY_RUN)

    traded, trade_data = has_traded_in_session(session)
    breakouts = get_session_breakouts(client)
    balance = client.get_balance() or 0.0

    lines = [
        f"{emoji} *{name} Session Abschluss*{dry_tag}",
        "",
    ]

    # Breakout-Check
    lines.append("*Breakout-Check:*")
    any_breakout = False
    for asset in ASSETS:
        if asset not in breakouts:
            lines.append(f"  • {asset}: ⚠️ Keine Box-Daten")
            continue
        b = breakouts[asset]
        if b["status"] == "breakout":
            any_breakout = True
            direction_icon = "🟢" if b["direction"] == "long" else "🔴"
            lines.append(f"  • {asset}: {direction_icon} *{b['direction'].upper()}* (${b['breakout_size']:.2f})")
        elif b["status"] == "error":
            lines.append(f"  • {asset}: ⚠️ Preis-Fehler")
        else:
            lines.append(f"  • {asset}: ✅ Kein Breakout")

    lines.append("")

    # Trade Status
    if traded:
        asset = trade_data.get("asset", "?")
        direction = trade_data.get("direction", "?").upper()
        entry = trade_data.get("entry_price", 0)
        direction_icon = "🟢" if trade_data.get("direction") == "long" else "🔴"
        lines.append(f"*Trade:* ✅ {direction_icon} *{asset} {direction}* @ ${entry:,.4f}")
    else:
        if any_breakout:
            positions = client.get_positions()
            if positions:
                pos = positions[0]
                pos_dir = "LONG" if pos.size > 0 else "SHORT"
                lines.append(f"*Trade:* ⏭️ Geskippt")
                lines.append(f"*Grund:* Position offen ({pos.coin} {pos_dir})")
            else:
                lines.append(f"*Trade:* ❌ Nicht ausgeführt")
                lines.append(f"*Grund:* Unbekannt – Script-Check nötig!")
        else:
            lines.append(f"*Trade:* ✅ Korrekt geskippt (kein Breakout)")

    lines.append("")

    # Balance & P&L
    lines.append(f"*Balance:* ${balance:,.2f} USDT")
    pnl = balance - CAPITAL
    if CAPITAL > 0:
        pnl_pct = (pnl / CAPITAL) * 100
        pnl_icon = "📈" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""
        lines.append(f"*P&L vs Start:* {pnl_icon} {pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.2f}%)")
    lines.append(f"_(Startkapital: ${CAPITAL:.2f} USDT)_")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: session_summary.py <tokyo|eu|us>")
        sys.exit(1)

    session = sys.argv[1].lower()
    if session not in ["tokyo", "eu", "us"]:
        print(f"Ungültige Session: {session}")
        sys.exit(1)

    summary = format_summary(session)
    print(summary)
    send_telegram_message(summary)
    print("NO_REPLY")


if __name__ == "__main__":
    main()
