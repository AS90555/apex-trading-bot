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
        ASSET_PRIORITY as ASSETS,
    )
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0
    MAX_RISK_PCT = 0.02
    ASSETS = ["ETH", "SOL", "AVAX", "XRP"]

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
    """Liefert den jüngsten Trade dieser Session am heutigen Tag (newest-first)."""
    if not os.path.exists(TRADES_FILE):
        return False, None
    try:
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False, None

    today = datetime.now().date().isoformat()
    for trade in reversed(trades):
        trade_date = trade.get("timestamp", "")[:10]
        if trade_date and trade_date < today:
            break
        trade_session = trade.get("session", "")
        if trade_date == today and trade_session == session:
            return True, trade
    return False, None


def format_summary(session):
    emoji = SESSION_EMOJIS.get(session, "📊")
    name = SESSION_NAMES.get(session, session.upper())
    dry_tag = " [DRY RUN]" if DRY_RUN else ""

    client = BitgetClient(dry_run=DRY_RUN)
    traded, trade_data = has_traded_in_session(session)
    equity = client.get_balance()

    lines = [
        f"{emoji} *{name} Session Abschluss*{dry_tag}",
        "",
    ]

    # Trade Status — basiert auf Trade-Log, nicht auf aktuellem Marktpreis
    if traded:
        asset = trade_data.get("asset", "?")
        direction = trade_data.get("direction", "?").upper()
        entry = trade_data.get("entry_price", 0)
        sl = trade_data.get("stop_loss", 0)
        tp1 = trade_data.get("take_profit_1", 0)
        direction_icon = "🟢" if trade_data.get("direction") == "long" else "🔴"
        lines.append(f"*Trade:* ✅ {direction_icon} *{asset} {direction}* @ ${entry:,.4f}")
        lines.append(f"  SL: ${sl:,.4f} | TP1: ${tp1:,.4f}")
        # Exit-Ergebnis falls Trade während der Session schon geschlossen wurde
        exit_pnl = trade_data.get("exit_pnl_usd")
        if exit_pnl is not None:
            exit_r = trade_data.get("exit_pnl_r", 0)
            reason = trade_data.get("exit_reason", "")
            result_icon = "✅" if exit_pnl > 0 else ("⚖️" if exit_pnl == 0 else "❌")
            sign = "+" if exit_pnl >= 0 else ""
            lines.append(f"  Ergebnis: {result_icon} {sign}${exit_pnl:.2f} ({sign}{exit_r}R) {reason}")
    else:
        positions = client.get_positions()
        if positions:
            pos = positions[0]
            pos_dir = "LONG" if pos.size > 0 else "SHORT"
            pnl_icon = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
            lines.append(f"*Trade:* ⏭️ Geskippt (Position offen)")
            lines.append(f"  {pos.coin} {pos_dir} @ ${pos.entry_price:,.4f} {pnl_icon} ${pos.unrealized_pnl:+,.2f}")
        else:
            lines.append(f"*Trade:* ✅ Kein Signal – kein Trade")

    lines.append("")

    # Balance & P&L
    lines.append(f"*Account Equity:* ${equity:,.2f} USDT")
    pnl = equity - CAPITAL
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
    from log_utils import setup_logging
    setup_logging()
    main()
