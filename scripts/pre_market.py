#!/usr/bin/env python3
"""
APEX - Pre-Market Check
========================
Prueft Balance, API-Verbindung und offene Positionen vor Session-Start.
Sendet Status-Report direkt an Telegram.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient
from telegram_sender import send_telegram_message

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import DRY_RUN, ASSETS
except ImportError:
    DRY_RUN = True
    ASSETS = ["ETH", "SOL", "AVAX", "XRP"]


SESSION_NAMES = {
    "eu": "Europa (London Open)",
    "us": "USA (NY Open)",
    "tokyo": "Tokyo"
}

SESSION_EMOJIS = {
    "eu": "\U0001f1ea\U0001f1fa",
    "us": "\U0001f1fa\U0001f1f8",
    "tokyo": "\U0001f30f"
}


def run_pre_market(session):
    """Pre-Market System Check"""
    emoji = SESSION_EMOJIS.get(session, "\U0001f4ca")
    name = SESSION_NAMES.get(session, session.upper())

    dry_tag = " · DRY RUN" if DRY_RUN else ""

    try:
        client = BitgetClient(dry_run=DRY_RUN)
        if not client.is_ready:
            send_telegram_message(f"{name}{dry_tag} — API nicht konfiguriert, .env.bitget fehlt.")
            return
    except Exception as e:
        send_telegram_message(f"{name}{dry_tag} — API-Verbindung fehlgeschlagen: {e}")
        return

    # Balance
    balance_str = "?"
    try:
        balance = client.get_balance()
        balance_str = f"${balance:,.2f} USDT"
    except Exception:
        pass

    # Offene Positionen
    pos_lines = []
    try:
        positions = client.get_positions()
        for pos in positions:
            direction = "LONG" if pos.size > 0 else "SHORT"
            sign = "+" if pos.unrealized_pnl >= 0 else ""
            pnl_icon = "📈" if pos.unrealized_pnl >= 0 else "📉"
            pos_lines.append(
                f"{pnl_icon} {pos.coin} {direction}  {sign}${pos.unrealized_pnl:.2f}"
            )
    except Exception:
        pass

    # Marktpreise
    price_parts = []
    try:
        for asset in ASSETS:
            p = client.get_price(asset)
            price_parts.append(f"{asset} ${p:,.2f}")
    except Exception:
        pass

    # Nachricht zusammenbauen
    pos_summary = ", ".join(pos_lines) if pos_lines else "keine offenen Positionen"
    price_summary = "  ·  ".join(price_parts) if price_parts else ""

    msg = (
        f"{emoji} {name} läuft{dry_tag}. Book bei {balance_str}. {pos_summary}."
        + (f"\n{price_summary}" if price_summary else "")
    )

    print(msg)
    send_telegram_message(msg)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: pre_market.py <eu|us|tokyo>")
        sys.exit(1)

    session = sys.argv[1].lower()
    if session not in ["eu", "us", "tokyo"]:
        print(f"Invalid session: {session}")
        sys.exit(1)

    from log_utils import setup_logging
    setup_logging()
    try:
        run_pre_market(session)
    except Exception as e:
        print(f"\U0001f4a5 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"Bot-Fehler (pre_market) — {e}")
        sys.exit(1)

    print("NO_REPLY")
