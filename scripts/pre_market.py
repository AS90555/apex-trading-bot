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

    lines = [f"{emoji} APEX Pre-Market: {name}\n"]

    # API Connection Check
    try:
        client = BitgetClient(dry_run=DRY_RUN)
        if not client.is_ready:
            lines.append("\u274c API: NICHT konfiguriert (config/.env.bitget fehlt)!")
            send_telegram_message("\n".join(lines))
            return
        mode = " [DRY RUN]" if DRY_RUN else ""
        lines.append(f"\u2705 Bitget API verbunden{mode}")
    except Exception as e:
        lines.append(f"\u274c API-Verbindung fehlgeschlagen: {e}")
        send_telegram_message("\n".join(lines))
        return

    # Balance
    try:
        balance = client.get_balance()
        lines.append(f"\U0001f4b0 Balance: ${balance:,.2f} USDT")
    except Exception as e:
        lines.append(f"\u26a0\ufe0f Balance-Check fehlgeschlagen: {e}")

    # Positions
    try:
        positions = client.get_positions()
        if positions:
            lines.append(f"\U0001f4ca Offene Positionen: {len(positions)}")
            for pos in positions:
                direction = "LONG" if pos.size > 0 else "SHORT"
                pnl_emoji = "\U0001f7e2" if pos.unrealized_pnl >= 0 else "\U0001f534"
                lines.append(
                    f"  {pos.coin} {direction} | Entry: ${pos.entry_price:,.2f} | "
                    f"{pnl_emoji} P&L: ${pos.unrealized_pnl:+,.2f}"
                )
        else:
            lines.append("\U0001f4ca Keine offenen Positionen")
    except Exception as e:
        lines.append(f"\u26a0\ufe0f Positions-Check fehlgeschlagen: {e}")

    # Price Check
    try:
        prices = []
        for asset in ASSETS:
            p = client.get_price(asset)
            prices.append(f"{asset}: ${p:,.4f}")
        lines.append("\n" + " | ".join(prices))
    except Exception as e:
        lines.append(f"\u26a0\ufe0f Preis-Check fehlgeschlagen: {e}")

    lines.append(f"\n\u2705 System bereit fuer {name}")

    msg = "\n".join(lines)
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

    try:
        run_pre_market(session)
    except Exception as e:
        print(f"\U0001f4a5 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"\U0001f4a5 APEX pre_market.py ERROR: {e}")
        sys.exit(1)

    print("NO_REPLY")
