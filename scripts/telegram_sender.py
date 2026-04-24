#!/usr/bin/env python3
"""
Telegram Message Sender
========================
Sendet Nachrichten direkt an Telegram ohne Agent.
"""

import os
import requests
from datetime import datetime, timezone
from pathlib import Path

def load_telegram_config():
    """Load Telegram config from .env file"""
    config = {}
    env_file = Path(__file__).parent.parent / ".env.telegram"
    
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    config[key.strip()] = value.strip()
    
    return config

def send_telegram_message(message: str, parse_mode: str = None) -> bool:
    """
    Send message directly to Telegram (plain text, kein Markdown-Parsing).

    Returns:
        True if sent successfully
    """
    try:
        config = load_telegram_config()

        bot_token = config.get('TELEGRAM_BOT_TOKEN')
        chat_id = config.get('TELEGRAM_CHAT_ID')

        if not bot_token or bot_token == 'your_token_here':
            print("⚠️  TELEGRAM_BOT_TOKEN not configured in .env.telegram")
            return False

        if not chat_id:
            print("⚠️  TELEGRAM_CHAT_ID not configured")
            return False

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        data = {
            "chat_id": chat_id,
            "text": message,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        
        response = requests.post(url, json=data, timeout=10)
        
        if response.status_code == 200:
            print("✅ Telegram message sent")
            return True
        else:
            print(f"⚠️  Telegram API error: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
            
    except Exception as e:
        print(f"⚠️  Telegram send error: {e}")
        return False

# ─── Event-Tagging ────────────────────────────────────────────────────────────
# Standardisiertes Header-Format für alle Bot-Nachrichten.
# Verwendung in Bots:
#   from scripts.telegram_sender import format_event_tag, send_telegram_message
#   header = format_event_tag("KDT", "SIGNAL", "ETH")
#   send_telegram_message(f"{header}\n{body}")

EVENT_ICONS = {
    "SIGNAL": "🔔",
    "ENTRY":  "🔴",
    "EXIT":   "🟢",
    "ERROR":  "⚠️",
    "INFO":   "ℹ️",
}

def format_event_tag(bot: str, event: str, asset: str = "", dry_run: bool = False) -> str:
    """
    Gibt einen standardisierten Einzeiler zurück:
      🔔 [ APEX · KDT · SIGNAL · ETH · 06:00 UTC ]  [DRY]
    """
    ts    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    icon  = EVENT_ICONS.get(event.upper(), "•")
    parts = ["APEX", bot.upper(), event.upper()]
    if asset:
        parts.append(asset)
    parts.append(ts)
    tag = f"{icon} [ {' · '.join(parts)} ]"
    if dry_run:
        tag += "  [DRY]"
    return tag


if __name__ == "__main__":
    # Test
    import sys
    if len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])
        send_telegram_message(message)
    else:
        send_telegram_message("🧪 Test-Nachricht vom APEX Trading Bot")
