#!/usr/bin/env python3
"""
Telegram Message Sender
========================
Sendet Nachrichten direkt an Telegram ohne Agent.
"""

import os
import requests
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

if __name__ == "__main__":
    # Test
    import sys
    if len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])
        send_telegram_message(message)
    else:
        send_telegram_message("🧪 Test-Nachricht vom APEX Trading Bot")
