#!/usr/bin/env python3
"""
APEX - Session Context Restore
================================
Gibt Claude Code am Session-Start den vollständigen Kontext über den Bot-Zustand.

Befehl (Session Start):
    ! python /root/apex-trading-bot/scripts/apex_status.py

Dieser Befehl:
  - Zeigt Balance + offene Positionen
  - Zeigt aktive SL/TP Orders
  - Zeigt letzte Trades mit Entry- und Exit-Daten
  - Zeigt P&L Statistik
  - Zeigt Systemstatus (Crontab, Logs)
"""

import os
import sys
import json
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")
PNL_FILE = os.path.join(DATA_DIR, "pnl_tracker.json")

sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))

try:
    from bot_config import DRY_RUN, CAPITAL, LEVERAGE, MAX_RISK_PCT, ASSET_PRIORITY
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0
    LEVERAGE = 5
    MAX_RISK_PCT = 0.02
    ASSET_PRIORITY = ["ETH", "SOL", "AVAX", "XRP"]

SEP = "=" * 62


def fmt_pnl(val):
    if val is None:
        return "?"
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


def load_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_pnl():
    if not os.path.exists(PNL_FILE):
        return {}
    try:
        with open(PNL_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def get_session():
    h = datetime.now().hour
    if 2 <= h < 4:
        return "TOKYO"
    if 8 <= h < 11:
        return "EU"
    if 21 <= h < 23:
        return "US"
    return "Zwischen Sessions"


def print_api_status():
    """Versucht Live-Daten von Bitget zu holen. Graceful fallback wenn nicht erreichbar."""
    try:
        from scripts.bitget_client import BitgetClient
        client = BitgetClient(dry_run=DRY_RUN)
        if not client.is_ready:
            print("⚠️  API-Credentials nicht geladen – nur lokale Daten verfügbar\n")
            return None

        balance = client.get_balance()
        positions = client.get_positions()

        print(f"💰 Balance:     ${balance:,.2f} USDT")
        print(f"📊 Modus:       {'DRY RUN ⚠️' if DRY_RUN else 'LIVE 🔴'} | Hebel: {LEVERAGE}x | Risk: {MAX_RISK_PCT*100:.0f}%")

        if positions:
            print(f"\n📈 OFFENE POSITIONEN:")
            for pos in positions:
                direction = "LONG" if pos.size > 0 else "SHORT"
                pnl_str = fmt_pnl(pos.unrealized_pnl)
                print(f"   {pos.coin} {direction} | Size: {abs(pos.size)} | Entry: ${pos.entry_price:,.4f} | PnL: {pnl_str}")

            # Aktive SL/TP Orders
            for pos in positions:
                orders = client.get_tpsl_orders(pos.coin)
                if orders:
                    print(f"\n🎯 AKTIVE ORDERS ({pos.coin}):")
                    for o in orders:
                        plan = o.get("planType", "?")
                        trigger = float(o.get("triggerPrice", 0))
                        size = o.get("size", "?")
                        label = {"loss_plan": "SL", "profit_plan": "TP", "moving_plan": "TRAILING"}.get(plan, plan)
                        callback = o.get("callbackRatio", "")
                        cb_str = f" | Callback {float(callback)*100:.2f}%" if callback else ""
                        print(f"   {label}: ${trigger:,.4f}{cb_str} (Size {size})")
        else:
            print(f"\n📈 OFFENE POSITIONEN: keine")

        return client
    except Exception as e:
        print(f"⚠️  API-Fehler: {e}\n")
        return None


def print_trade_history():
    trades = load_trades()
    if not trades:
        print("   (keine Trades)")
        return

    # Statistik
    closed = [t for t in trades if t.get("exit_pnl_usd") is not None]
    open_trades = [t for t in trades if not t.get("exit_timestamp")]
    wins = [t for t in closed if t.get("exit_pnl_usd", 0) > 0]
    losses = [t for t in closed if t.get("exit_pnl_usd", 0) <= 0]
    total_pnl = sum(t.get("exit_pnl_usd", 0) for t in closed)
    winrate = len(wins) / len(closed) * 100 if closed else 0

    print(f"\n📊 P&L ÜBERSICHT:")
    print(f"   Trades gesamt: {len(trades)} | Abgeschlossen: {len(closed)} | Offen: {len(open_trades)}")
    if closed:
        print(f"   Wins: {len(wins)} | Losses: {len(losses)} | Winrate: {winrate:.0f}%")
        print(f"   Realisierter P&L: {fmt_pnl(total_pnl)}")
        avg_win = sum(t.get("exit_pnl_r", 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.get("exit_pnl_r", 0) for t in losses) / len(losses) if losses else 0
        print(f"   Avg Win: {avg_win:+.2f}R | Avg Loss: {avg_loss:+.2f}R")

    # Letzte 8 Trades
    print(f"\n📋 LETZTE 8 TRADES:")
    header = f"   {'Datum':<17} {'Asset':<5} {'Dir':<6} {'Entry':>10} {'SL':>10} {'Vol-Ratio':>9} {'PnL':>9} {'R':>6} {'Status'}"
    print(header)
    print("   " + "-" * 85)
    for t in trades[-8:]:
        ts = t.get("timestamp", "")[:16].replace("T", " ")
        asset = t.get("asset", "?")
        direction = t.get("direction", "?")[:5].upper()
        entry = t.get("entry_price", 0)
        sl = t.get("stop_loss", 0)
        vol_ratio = t.get("volume_ratio")
        vol_str = f"{vol_ratio:.2f}x" if vol_ratio is not None else "  N/A"
        pnl = t.get("exit_pnl_usd")
        pnl_r = t.get("exit_pnl_r")
        reason = t.get("exit_reason", "offen")

        pnl_str = fmt_pnl(pnl) if pnl is not None else "   offen"
        r_str = f"{pnl_r:+.2f}R" if pnl_r is not None else "      "
        print(f"   {ts:<17} {asset:<5} {direction:<6} ${entry:>9,.2f} ${sl:>9,.2f} {vol_str:>9} {pnl_str:>9} {r_str:>6} {reason}")


def print_system_status():
    print(f"\n⚙️  SYSTEM STATUS:")

    # Monitor State
    state = load_state()
    if state:
        last_check = state.get("last_check", "?")[:16] if state.get("last_check") else "?"
        pos_count = state.get("last_position_count", 0)
        be = state.get("be_applied", False)
        coin = state.get("tracked_coin", "-")
        print(f"   Monitor: letzter Check {last_check} | Positionen: {pos_count} | BE angewendet: {be}")
        if coin and coin != "-" and pos_count > 0:
            print(f"   Getracktes Asset: {coin}")

    # Crontab Check
    try:
        import subprocess
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        monitor_line = [l for l in cron.stdout.splitlines() if "position_monitor" in l]
        if monitor_line:
            interval = monitor_line[0].split()[0]
            print(f"   Position Monitor Cron: {interval} ✅")
    except Exception:
        pass

    # Log-Größen
    if os.path.exists(LOGS_DIR):
        logs = []
        for f in os.listdir(LOGS_DIR):
            fp = os.path.join(LOGS_DIR, f)
            if os.path.isfile(fp):
                size_kb = os.path.getsize(fp) / 1024
                logs.append(f"{f}: {size_kb:.0f}KB")
        if logs:
            print(f"   Logs: {' | '.join(logs)}")


def print_claude_context():
    """Liest relevante Abschnitte aus CLAUDE.md – letzte Session + offene Punkte."""
    claude_md = os.path.join(PROJECT_DIR, "CLAUDE.md")
    if not os.path.exists(claude_md):
        return
    try:
        with open(claude_md) as f:
            content = f.read()

        # Letzten Architektur-Abschnitt extrahieren
        lines = content.splitlines()
        capture = False
        output = []
        for line in lines:
            if "## Architektur-Entscheidungen" in line:
                capture = True
            elif line.startswith("## ") and capture and "Architektur" not in line:
                break
            if capture:
                output.append(line)

        if output:
            print(f"\n📋 LETZTER SESSION-LOG (aus CLAUDE.md):")
            # Nur die letzten 25 Zeilen zeigen (kompakt)
            relevant = [l for l in output if l.strip()][-25:]
            for line in relevant:
                print(f"   {line}")

        # "Was noch zu tun ist" Sektion
        todo_capture = False
        todo_lines = []
        for line in lines:
            if "noch zu tun" in line.lower() or "nächste" in line.lower() and "schritt" in line.lower():
                todo_capture = True
            elif line.startswith("## ") and todo_capture:
                break
            if todo_capture and line.strip():
                todo_lines.append(line)

        if todo_lines:
            print(f"\n🎯 OFFENE PUNKTE:")
            for line in todo_lines[:15]:
                print(f"   {line}")

    except Exception as e:
        print(f"⚠️  CLAUDE.md Lesefehler: {e}")


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session = get_session()

    print(SEP)
    print(f"  APEX SESSION CONTEXT – {now} | {session}")
    print(SEP)

    print_api_status()
    print_trade_history()
    print_claude_context()
    print_system_status()

    print(f"\n📁 CLAUDE.md: {os.path.join(PROJECT_DIR, 'CLAUDE.md')}")
    print(f"📁 Trades:    {TRADES_FILE}")
    print(SEP)


if __name__ == "__main__":
    main()
