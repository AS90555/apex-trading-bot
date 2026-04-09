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
    """Erstelle und sende Tages-Report"""
    client = BitgetClient(dry_run=DRY_RUN)

    lines = ["\U0001f4c8 APEX Tages-Abschluss\n"]

    # Balance
    balance = client.get_balance()
    mode = " [DRY RUN]" if DRY_RUN else ""
    lines.append(f"\U0001f4b0 Balance: ${balance:,.2f} USDT{mode}")

    # Drawdown-Timeline pflegen (ein Snapshot pro Kalendertag, idempotent)
    append_drawdown_snapshot(balance)

    # Gesamt P&L
    capital = get_capital_tracking()
    adjusted_start = capital["adjusted_start_capital"]
    pnl = balance - adjusted_start
    pnl_pct = (pnl / adjusted_start) * 100 if adjusted_start > 0 else 0

    if pnl >= 0:
        lines.append(f"\U0001f4c8 Gesamt P&L: +${pnl:.2f} ({pnl_pct:+.2f}%)")
    else:
        lines.append(f"\U0001f4c9 Gesamt P&L: -${abs(pnl):.2f} ({pnl_pct:+.2f}%)")

    lines.append(f"(Start: ${adjusted_start:,.2f})")

    # Heutige Trades
    todays_trades = get_todays_trades()
    lines.append(f"\n\U0001f4cb Trades heute: {len(todays_trades)}")

    for trade in todays_trades:
        asset = trade.get("asset", "?")
        direction = trade.get("direction", "?").upper()
        entry = trade.get("entry_price", 0)
        session = trade.get("session", "?")
        lines.append(f"  {asset} {direction} @ ${entry:,.2f} ({session})")

    # Offene Positionen
    positions = client.get_positions()
    if positions:
        lines.append(f"\n\U0001f4ca Offene Positionen: {len(positions)}")
        for pos in positions:
            direction = "LONG" if pos.size > 0 else "SHORT"
            pnl_emoji = "\U0001f7e2" if pos.unrealized_pnl >= 0 else "\U0001f534"
            lines.append(
                f"  {pos.coin} {direction} | Entry: ${pos.entry_price:,.2f} | "
                f"{pnl_emoji} ${pos.unrealized_pnl:+,.2f}"
            )
    else:
        lines.append(f"\n\U0001f4ca Keine offenen Positionen")

    # P&L Tracker Stats
    tracker = get_pnl_tracker()
    if tracker:
        total = tracker.get("total_trades", 0)
        wins = tracker.get("winning_trades", 0)
        losses = tracker.get("losing_trades", 0)
        if total > 0:
            win_rate = (wins / total) * 100
            lines.append(f"\n\U0001f3af Stats: {wins}W/{losses}L ({win_rate:.0f}% Win-Rate)")

    msg = "\n".join(lines)
    print(msg)
    send_telegram_message(msg)


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
