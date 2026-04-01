#!/usr/bin/env python3
"""
APEX - Save Opening Range Boxes
================================
Speichert High/Low der ersten 15 Min für spätere Breakout-Checks
"""

import os
import sys
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient
from telegram_sender import send_telegram_message

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
BOXES_FILE = os.path.join(DATA_DIR, "opening_range_boxes.json")

# Config laden
sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import DRY_RUN, ASSETS
except ImportError:
    DRY_RUN = True
    ASSETS = ["ETH", "SOL", "AVAX"]


def save_opening_range():
    """Hole und speichere Opening Range für alle Assets"""
    client = BitgetClient(dry_run=DRY_RUN)

    assets = ASSETS
    boxes = {}
    
    print("=" * 60)
    print("APEX - Opening Range Capture")
    print("=" * 60)
    
    for asset in assets:
        # limit=2: candles sortiert oldest-first → candles[0] = abgeschlossene Kerze
        # candles[1] = aktuell laufende Kerze (Range=0 → unbrauchbar)
        candles = client.get_candles(asset, "15m", limit=2)
        if len(candles) < 2:
            time.sleep(3)
            candles = client.get_candles(asset, "15m", limit=2)

        if len(candles) < 2:
            print(f"⚠️  No candles for {asset}")
            continue

        candle = candles[0]  # abgeschlossene (nicht aktuell laufende) Kerze
        
        boxes[asset] = {
            "high": candle["high"],
            "low": candle["low"],
            "open": candle["open"],
            "close": candle["close"],
            "timestamp": datetime.now().isoformat()
        }
        
        print(f"\n📊 {asset}:")
        print(f"   High: ${candle['high']:,.2f}")
        print(f"   Low:  ${candle['low']:,.2f}")
        print(f"   Range: ${candle['high'] - candle['low']:,.2f}")
    
    # Save
    os.makedirs(os.path.dirname(BOXES_FILE), exist_ok=True)
    with open(BOXES_FILE, 'w') as f:
        json.dump(boxes, f, indent=2)
    
    print(f"\n✅ Boxes saved to {BOXES_FILE}")

    # Send Telegram notification
    lines = ["📊 APEX Opening Range Captured\n"]
    for asset in assets:
        if asset in boxes:
            b = boxes[asset]
            rng = b["high"] - b["low"]
            lines.append(f"{asset}: ${b['high']:,.2f} / ${b['low']:,.2f} (Range: ${rng:,.2f})")
    send_telegram_message("\n".join(lines))

    return boxes


if __name__ == "__main__":
    try:
        boxes = save_opening_range()
        print("NO_REPLY")
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"💥 APEX save_opening_range.py ERROR: {e}")
        print("NO_REPLY")
        sys.exit(1)
