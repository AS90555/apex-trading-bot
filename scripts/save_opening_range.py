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
from config.bot_config import PRICE_DECIMALS

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
BOXES_FILE = os.path.join(DATA_DIR, "opening_range_boxes.json")

# Config laden
sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import DRY_RUN, ASSETS, MIN_BOX_RANGE
except ImportError:
    DRY_RUN = True
    ASSETS = ["ETH", "SOL", "AVAX", "XRP"]
    MIN_BOX_RANGE = {"ETH": 1.0, "SOL": 0.10, "AVAX": 0.04, "XRP": 0.003}


def save_opening_range():
    """Hole und speichere Opening Range für alle Assets"""
    client = BitgetClient(dry_run=DRY_RUN)

    assets = ASSETS
    boxes = {}
    
    print("=" * 60)
    print("APEX - Opening Range Capture")
    print("=" * 60)
    
    for asset in assets:
        try:
            # limit=5: candles sortiert oldest-first
            # candles[-1] = aktuell laufende Kerze (unbrauchbar)
            # candles[-2] = zuletzt abgeschlossene Kerze
            candles = client.get_candles(asset, "15m", limit=5)
            if len(candles) < 2:
                time.sleep(3)
                candles = client.get_candles(asset, "15m", limit=5)

            if len(candles) < 2:
                print(f"⚠️  No candles for {asset}")
                continue

            candle = candles[-2]  # zuletzt abgeschlossene Kerze (nicht laufende)

            box_range = candle["high"] - candle["low"]
            min_range = MIN_BOX_RANGE.get(asset, candle["high"] * 0.0005)
            if box_range < min_range:
                print(f"⚠️  {asset}: Box Range ${box_range:.4f} < Min ${min_range:.4f} – übersprungen")
                continue

            boxes[asset] = {
                "high": candle["high"],
                "low": candle["low"],
                "open": candle["open"],
                "close": candle["close"],
                "timestamp": datetime.now().isoformat()
            }

            rng_display = candle['high'] - candle['low']
            decimals = PRICE_DECIMALS.get(asset, 2)
            fmt = f",.{decimals}f"
            print(f"\n📊 {asset}:")
            print(f"   High: ${candle['high']:{fmt}}")
            print(f"   Low:  ${candle['low']:{fmt}}")
            print(f"   Range: ${rng_display:{fmt}}")
        except Exception as e:
            print(f"⚠️  {asset}: Fehler beim Laden der Candles: {e}")
            continue

        time.sleep(1)  # Rate Limit Schutz
    
    # Save (atomar: tmp + rename verhindert korrupte JSON bei Crash mid-write)
    os.makedirs(os.path.dirname(BOXES_FILE), exist_ok=True)
    tmp_file = BOXES_FILE + ".tmp"
    with open(tmp_file, 'w') as f:
        json.dump(boxes, f, indent=2)
    os.replace(tmp_file, BOXES_FILE)

    print(f"\n✅ Boxes saved to {BOXES_FILE}")

    # Send Telegram notification
    lines = ["📊 APEX Opening Range Captured\n"]
    for asset in assets:
        if asset in boxes:
            b = boxes[asset]
            rng = b["high"] - b["low"]
            decimals = PRICE_DECIMALS.get(asset, 2)
            fmt = f",.{decimals}f"
            lines.append(f"{asset}: ${b['high']:{fmt}} / ${b['low']:{fmt}} (Range: ${rng:{fmt}})")
    send_telegram_message("\n".join(lines))

    return boxes


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()
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
