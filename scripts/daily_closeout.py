#!/usr/bin/env python3
"""
APEX - Daily Closeout Report
==============================
Erstellt Tages-Report mit Balance, Trades, P&L.
Sendet direkt an Telegram.
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
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
CAPITAL_FILE = os.path.join(DATA_DIR, "capital_tracking.json")
PNL_TRACKER_FILE = os.path.join(DATA_DIR, "pnl_tracker.json")
HWM_FILE = os.path.join(DATA_DIR, "high_water_mark.json")
PENDING_NOTES_FILE = os.path.join(DATA_DIR, "pending_notes.jsonl")
DEEP_REVIEW_FLAG_FILE = os.path.join(DATA_DIR, "deep_review_pending.flag")
HYPOTHESIS_LOG = "/root/.claude/projects/-root-apex-trading-bot/memory/hypothesis_log.md"

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import DRY_RUN, CAPITAL
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0


def get_capital_tracking():
    """Lade Capital Tracking"""
    if not os.path.exists(CAPITAL_FILE):
        return {
            "start_capital": CAPITAL,
            "adjusted_start_capital": CAPITAL,
            "total_deposits": 0,
            "total_withdrawals": 0,
        }
    with open(CAPITAL_FILE, "r") as f:
        return json.load(f)


def get_todays_trades():
    """Hole alle Trades von heute"""
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE, 'r') as f:
        trades = json.load(f)

    today = datetime.now().date().isoformat()
    return [t for t in trades if t.get("timestamp", "")[:10] == today]


def get_pnl_tracker():
    """Lade P&L Tracker"""
    if not os.path.exists(PNL_TRACKER_FILE):
        return None
    with open(PNL_TRACKER_FILE, 'r') as f:
        return json.load(f)


def append_drawdown_snapshot(balance: float):
    """Schreibt einen täglichen Balance/HWM/DD-Snapshot in high_water_mark.json.

    Struktur:
      {
        "hwm": <float>,                     # legacy, bleibt für Kompatibilität
        "updated": <iso>,                   # legacy
        "history": [
          {"date": "YYYY-MM-DD", "balance": .., "hwm": .., "dd_pct": ..},
          ...
        ]
      }
    Pro Kalendertag genau ein Eintrag (idempotent – überschreibt bei wiederholtem Aufruf).
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        state = {"hwm": CAPITAL, "updated": None, "history": []}
        if os.path.exists(HWM_FILE):
            try:
                with open(HWM_FILE, "r") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    state.update(loaded)
                    if "history" not in state or not isinstance(state["history"], list):
                        state["history"] = []
            except (json.JSONDecodeError, OSError) as e:
                print(f"⚠️  high_water_mark.json unlesbar ({e}) – re-initialisiere")

        old_hwm = float(state.get("hwm") or CAPITAL)
        new_hwm = max(old_hwm, float(balance))
        dd_pct = round(((new_hwm - balance) / new_hwm) * 100, 3) if new_hwm > 0 else 0.0

        today = datetime.now().date().isoformat()
        snapshot = {
            "date": today,
            "balance": round(float(balance), 4),
            "hwm": round(new_hwm, 4),
            "dd_pct": dd_pct,
        }

        # Idempotenz: existierenden Tages-Eintrag überschreiben statt duplizieren
        history = [h for h in state["history"] if h.get("date") != today]
        history.append(snapshot)
        state["history"] = history
        state["hwm"] = round(new_hwm, 4)
        state["updated"] = datetime.now().isoformat()

        tmp_file = HWM_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, HWM_FILE)
        print(f"📉 Drawdown-Snapshot: {snapshot}")
    except Exception as e:
        print(f"⚠️  Drawdown-Snapshot Fehler: {e}")


def run_daily_closeout():
    """Silent-Mode: Nur Drawdown-Snapshot persistieren.

    Telegram-Report läuft jetzt komplett in nightly_report.py (01:30 Uhr).
    Dieses Skript bleibt aktiv als Safety-Net: falls Nightly fällt, ist der
    Tages-Snapshot trotzdem in high_water_mark.json geschrieben.
    """
    client = BitgetClient(dry_run=DRY_RUN)
    balance = client.get_balance()
    append_drawdown_snapshot(balance)
    print(f"✅ Daily Snapshot persistiert (Balance ${balance:.2f})")


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()
    try:
        run_daily_closeout()
    except Exception as e:
        print(f"\U0001f4a5 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"\U0001f4a5 APEX daily_closeout.py ERROR: {e}")
        sys.exit(1)

    print("NO_REPLY")
