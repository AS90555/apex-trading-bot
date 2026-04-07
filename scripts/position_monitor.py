#!/usr/bin/env python3
"""
APEX - Position Monitor
=======================
Checkt ob Positionen geschlossen wurden und meldet Ergebnisse.
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")
PNL_TRACKER_FILE = os.path.join(DATA_DIR, "pnl_tracker.json")

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import DRY_RUN
except ImportError:
    DRY_RUN = True


def load_state():
    """Load last known state"""
    if not os.path.exists(STATE_FILE):
        return {"last_position_count": 0, "last_check": None}
    with open(STATE_FILE, 'r') as f:
        return json.load(f)


def save_state(state):
    """Save current state"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def get_total_trade_pnl(client, coin: str, opened_at_ms: int):
    """Summiert P&L aller Fills seit Position-Eröffnung (TP1 + TP2/SL).
    Returns: (total_pnl, exit_price, total_size)
    """
    fills = client.get_recent_fills(coin=coin, limit=20) if coin else client.get_recent_fills(limit=10)
    total_pnl = 0.0
    total_size = 0.0
    exit_price = 0.0

    for fill in fills:
        fill_time = int(fill.get("cTime", 0))
        if opened_at_ms and fill_time < opened_at_ms:
            break  # ältere Fills gehören nicht zum aktuellen Trade
        total_pnl += float(fill.get("profit", 0))
        size = float(fill.get("baseVolume", fill.get("size", fill.get("fillSz", 0))))
        total_size += size
        if not exit_price:
            exit_price = float(fill.get("price", 0))

    return total_pnl, exit_price, total_size


def send_telegram_notification(message):
    """Send notification via telegram_sender module"""
    try:
        from telegram_sender import send_telegram_message
        send_telegram_message(message)
    except Exception as e:
        print(f"⚠️  Telegram notification error: {e}")


def update_pnl_tracker(pnl):
    """Update P&L tracker with realized profit"""
    if not os.path.exists(PNL_TRACKER_FILE):
        return
    
    with open(PNL_TRACKER_FILE, 'r') as f:
        tracker = json.load(f)
    
    # Update realized P&L
    tracker["realized_pnl"] = tracker.get("realized_pnl", 0) + pnl
    tracker["total_pnl"] = tracker["realized_pnl"] + tracker.get("unrealized_pnl", 0)
    
    # Update trade counts
    if pnl > 0:
        tracker["winning_trades"] = tracker.get("winning_trades", 0) + 1
    else:
        tracker["losing_trades"] = tracker.get("losing_trades", 0) + 1
    
    tracker["total_trades"] = tracker.get("total_trades", 0) + 1
    tracker["last_updated"] = datetime.now().isoformat()
    
    # Check milestones
    for milestone_name, milestone in tracker.get("milestones", {}).items():
        if not milestone.get("reached", False):
            if tracker["total_pnl"] >= milestone["target"]:
                milestone["reached"] = True
                print(f"\n🎉 MILESTONE REACHED: +${milestone['target']} → Bonus: +${milestone['bonus']} USDC!")
    
    with open(PNL_TRACKER_FILE, 'w') as f:
        json.dump(tracker, f, indent=2)


def main():
    """Main monitoring logic"""
    client = BitgetClient(dry_run=DRY_RUN)

    positions = client.get_positions()
    current_count = len(positions)
    state = load_state()
    last_count = state.get("last_position_count", 0)

    if current_count == 0 and last_count == 0:
        print("\n⏸️  Keine Positionen - Monitor idle")
        return current_count

    new_state = {"last_position_count": current_count, "last_check": datetime.now().isoformat()}

    if last_count > 0 and current_count == 0:
        # Position geschlossen — alle Fills seit Eröffnung summieren
        tracked_coin = state.get("tracked_coin")
        opened_at_ms = state.get("position_opened_at", 0)

        print("\n" + "=" * 60)
        print("🎯 POSITION GESCHLOSSEN!")
        print("=" * 60)

        total_pnl, exit_price, total_size = get_total_trade_pnl(client, tracked_coin, opened_at_ms)
        coin = tracked_coin or "?"

        if exit_price:
            print(f"\n💰 FINAL RESULT:")
            print(f"   Asset: {coin}")
            print(f"   Exit:  ${exit_price:,.4f}")
            print(f"   Size:  {total_size:.4f}")
            print(f"   P&L:   ${total_pnl:,.2f}")

            balance = client.get_balance()
            print(f"\nAktuelle Balance: ${balance:,.2f} USDT")

            emoji = "✅" if total_pnl > 0 else "❌"
            result_text = f"GEWINN: +${total_pnl:.2f}" if total_pnl > 0 else f"VERLUST: ${total_pnl:.2f}"

            message = (
                f"🎯 APEX TRADE GESCHLOSSEN!\n\n"
                f"{emoji} {result_text}\n\n"
                f"Asset: {coin}\n"
                f"Exit: ${exit_price:,.4f}\n"
                f"Size: {total_size:.4f}\n\n"
                f"💰 Neue Balance: ${balance:,.2f} USDT"
            )
            print(f"\n{emoji} {result_text}")
            send_telegram_notification(message)
            update_pnl_tracker(total_pnl)
        else:
            print("⚠️  Keine Fill-Daten verfügbar")
            send_telegram_notification("🎯 APEX: Position geschlossen, aber keine Trade-Details gefunden.")

    elif current_count > 0:
        pos = positions[0]
        print(f"\n✅ Position läuft weiter:")
        print(f"   {pos.coin} {'LONG' if pos.size > 0 else 'SHORT'}")
        print(f"   P&L: ${pos.unrealized_pnl:.2f}")

        # Position-Tracking: opened_at und coin merken für späteren P&L
        if last_count == 0 or "position_opened_at" not in state:
            new_state["position_opened_at"] = int(datetime.now().timestamp() * 1000)
            new_state["tracked_coin"] = pos.coin
        else:
            new_state["position_opened_at"] = state.get("position_opened_at")
            new_state["tracked_coin"] = state.get("tracked_coin", pos.coin)
    else:
        print("\n⏸️  Keine offenen Positionen")

    save_state(new_state)
    return current_count


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()
    try:
        count = main()
        print("NO_REPLY")
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()
        print("NO_REPLY")
        sys.exit(1)
