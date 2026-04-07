#!/usr/bin/env python3
"""
APEX - Nightly Performance Report
===================================
Läuft täglich um 01:30 Uhr, analysiert alle Sessions des Tages
und sendet einen strukturierten Bericht an Telegram.
"""

import os
import sys
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telegram_sender import send_telegram_message

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR    = os.path.join(PROJECT_DIR, "data")
LOGS_DIR    = os.path.join(PROJECT_DIR, "logs")

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import CAPITAL, BREAKOUT_THRESHOLD, MIN_BOX_RANGE
except ImportError:
    CAPITAL = 68.33
    BREAKOUT_THRESHOLD = {"ETH": 5.0, "SOL": 0.30, "AVAX": 0.15, "XRP": 0.005}
    MIN_BOX_RANGE      = {"ETH": 1.0,  "SOL": 0.10, "AVAX": 0.04,  "XRP": 0.003}

TODAY     = datetime.now().date()
YESTERDAY = TODAY - timedelta(days=1)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def tail_log(filename, lines=300):
    """Liest die letzten N Zeilen einer Log-Datei."""
    path = os.path.join(LOGS_DIR, filename)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", errors="replace") as f:
            return f.readlines()[-lines:]
    except OSError:
        return []


def parse_ts(line):
    """Extrahiert datetime aus '[YYYY-MM-DD HH:MM:SS] ...' Zeilen."""
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
    return datetime.fromisoformat(m.group(1)) if m else None


def filter_today(lines):
    """Gibt Zeilen zurück die heute oder gestern nach 23:00 geloggt wurden.
    Fallback: wenn keine Timestamps gefunden, alle letzten Zeilen zurückgeben
    (für Logs die noch mit altem Code ohne Timestamps geschrieben wurden).
    """
    result = []
    for line in lines:
        ts = parse_ts(line)
        if ts and ts.date() == TODAY:
            result.append((ts, line))
        elif ts and ts.date() == YESTERDAY and ts.hour >= 23:
            result.append((ts, line))
        elif not ts and result:
            result.append((None, line))  # Continuation-Zeile

    # Fallback: keine Timestamps → nur letzte 60 Zeilen (= aktuelle Session)
    if not result:
        return [(None, l) for l in lines[-60:]]
    return result


# ─── Session-Log-Analyse ──────────────────────────────────────────────────────

def analyse_session_log(logfile, session_name):
    """
    Analysiert ein Session-Log und extrahiert:
    - Ob ein Trade ausgeführt wurde
    - Breakout-Meldungen
    - Fehler / Warnungen
    - Box-Ranges
    """
    lines = tail_log(logfile)
    today_lines = filter_today(lines)

    result = {
        "name":      session_name,
        "traded":    False,
        "trade_txt": None,
        "breakouts": [],
        "no_signal": False,
        "errors":    [],
        "warnings":  [],
        "box_ranges": {},
    }

    for ts, line in today_lines:
        stripped = line.strip()

        # Trade ausgeführt
        if "TRADE AUSGEFÜHRT" in stripped or "TRADE EXECUTED" in stripped:
            result["traded"] = True
        if "Entry:" in stripped and result["traded"] and not result["trade_txt"]:
            result["trade_txt"] = stripped.replace("[", "").split("] ", 1)[-1]

        # Kein Breakout
        if "Kein Breakout" in stripped:
            result["no_signal"] = True

        # Breakout erkannt (aber ggf. nicht getradet)
        m = re.search(r"BREAKOUT.*?(\w+)\s+(LONG|SHORT)", stripped)
        if m:
            result["breakouts"].append(f"{m.group(1)} {m.group(2)}")

        # Box-Ranges
        asset_m = re.match(r".*📊\s+(\w+):", stripped)
        if asset_m:
            asset = asset_m.group(1)
            range_m = re.search(r"Range:\s*\$?([\d.]+)", stripped)
            if range_m:
                result["box_ranges"][asset] = float(range_m.group(1))

        # Fehler
        if any(x in stripped for x in ["💥 ERROR", "FEHLER:", "KRITISCH"]):
            result["errors"].append(stripped[-120:])

        # Warnungen
        if "⚠️" in stripped and "Rate Limit" not in stripped:
            result["warnings"].append(stripped[-100:])

    return result


# ─── Trade-History Analyse ─────────────────────────────────────────────────────

def analyse_trades(days=7):
    """Analysiert trades.json für die letzten N Tage."""
    trades = load_json(os.path.join(DATA_DIR, "trades.json"), [])
    cutoff = (datetime.now() - timedelta(days=days)).date()

    recent = []
    for t in trades:
        try:
            d = datetime.fromisoformat(t.get("timestamp", "")).date()
            if d >= cutoff:
                recent.append(t)
        except (ValueError, TypeError):
            pass

    today_trades = [t for t in recent
                    if datetime.fromisoformat(t.get("timestamp", "1970-01-01")).date() == TODAY]

    # Win/Loss aus monitor.log schätzen (über P&L-Tracker falls vorhanden)
    pnl_tracker = load_json(os.path.join(DATA_DIR, "pnl_tracker.json"), {})

    by_session = defaultdict(list)
    by_asset   = defaultdict(list)
    for t in recent:
        by_session[t.get("session", "?")].append(t)
        by_asset[t.get("asset", "?")].append(t)

    return {
        "today":      today_trades,
        "recent":     recent,
        "by_session": dict(by_session),
        "by_asset":   dict(by_asset),
        "pnl_tracker": pnl_tracker,
    }


# ─── Auffälligkeiten erkennen ──────────────────────────────────────────────────

def detect_anomalies(sessions, trades_data, boxes):
    """Findet auffällige Muster und Optimierungshinweise."""
    hints = []

    # Zu enge/weite Breakout-Thresholds
    for asset, box in (boxes or {}).items():
        rng = box.get("high", 0) - box.get("low", 0)
        threshold = BREAKOUT_THRESHOLD.get(asset, 0)
        if rng > 0 and threshold > 0:
            ratio = threshold / rng
            if ratio > 0.5:
                hints.append(f"⚡ {asset}: BREAKOUT_THRESHOLD (${threshold}) ist {ratio:.0%} der Box-Range — ggf. zu eng")
            elif ratio < 0.1:
                hints.append(f"⚡ {asset}: BREAKOUT_THRESHOLD sehr klein vs. Box-Range — viele False Signals möglich")

    # Breakout erkannt aber kein Trade
    for s in sessions:
        if s["breakouts"] and not s["traded"]:
            hints.append(f"⏰ {s['name']}: Breakout erkannt ({', '.join(s['breakouts'])}) aber kein Trade ausgeführt")

    # Fehler in Logs
    for s in sessions:
        if s["errors"]:
            hints.append(f"🚨 {s['name']}: {len(s['errors'])} Fehler in den Logs")

    # Keine Trades in mehreren Sessions
    tradeless = [s["name"] for s in sessions if not s["traded"] and not s["no_signal"]]
    if len(tradeless) >= 2:
        hints.append(f"📉 Kein Trade in {len(tradeless)} Sessions ({', '.join(tradeless)}) — Thresholds prüfen?")

    return hints


# ─── Report formatieren ────────────────────────────────────────────────────────

def format_report():
    # Logs einlesen
    tokyo = analyse_session_log("tokyo.log", "Tokyo 🌏")
    eu    = analyse_session_log("eu.log",    "EU 🇪🇺")
    us    = analyse_session_log("us.log",    "US 🇺🇸")

    # Trade-Daten
    td = analyse_trades(days=7)

    # Aktuelle Boxes
    boxes = load_json(os.path.join(DATA_DIR, "opening_range_boxes.json"), {})

    # Balance aus Daily-Log
    balance_txt = "unbekannt"
    hwm_txt = ""
    daily_lines = tail_log("daily.log", 50)
    for line in reversed(daily_lines):
        m = re.search(r"Balance:.*?\$([\d.,]+)", line)
        if m:
            balance_txt = f"${m.group(1)} USDT"
            break
    hwm = load_json(os.path.join(DATA_DIR, "high_water_mark.json"), {})
    if hwm.get("hwm"):
        hwm_txt = f" | HWM: ${hwm['hwm']:.2f}"

    # PnL
    pnl_line = ""
    for line in reversed(daily_lines):
        if "Gesamt P&L" in line or "P&L" in line:
            pnl_line = re.sub(r"\[.*?\]\s*", "", line).strip()
            break

    # Anomalien
    anomalies = detect_anomalies([tokyo, eu, us], td, boxes)

    # ── Report zusammenbauen ──
    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    day_str = f"{weekdays[TODAY.weekday()]} {TODAY.strftime('%d.%m.%Y')}"

    lines = [f"🌙 APEX Nachtbericht — {day_str}\n"]

    # Sessions
    lines.append("📊 Heutige Sessions")
    for s in [tokyo, eu, us]:
        if s["traded"]:
            trade_info = s["trade_txt"] or "Trade ausgeführt"
            lines.append(f"  ✅ {s['name']}: {trade_info}")
        elif s["breakouts"]:
            lines.append(f"  ⏰ {s['name']}: Breakout ({', '.join(s['breakouts'])}) – nicht getradet")
        elif s["no_signal"]:
            lines.append(f"  ➖ {s['name']}: Kein Signal")
        else:
            lines.append(f"  ❔ {s['name']}: Keine Daten")

    # Heutige Trades
    if td["today"]:
        lines.append("\n💼 Trades heute")
        for t in td["today"]:
            session = t.get("session", "?").upper()
            asset   = t.get("asset", "?")
            side    = "🟢 LONG" if t.get("direction") == "long" else "🔴 SHORT"
            entry   = t.get("entry_price", 0)
            risk    = t.get("risk_usd", 0)
            lines.append(f"  {side} {asset} @ ${entry:,.4f} ({session}, Risk: ${risk:.2f})")

    # Balance
    lines.append(f"\n💰 Kontostand")
    lines.append(f"  Balance: {balance_txt}{hwm_txt}")
    if pnl_line:
        lines.append(f"  {pnl_line}")

    # 7-Tage-Performance
    if td["recent"]:
        lines.append(f"\n📈 Letzte 7 Tage ({len(td['recent'])} Trades)")
        tracker = td["pnl_tracker"]
        if tracker:
            wins   = tracker.get("winning_trades", 0)
            losses = tracker.get("losing_trades", 0)
            total  = tracker.get("total_trades", 0)
            wr     = f"{wins/total*100:.0f}%" if total > 0 else "?"
            realized = tracker.get("realized_pnl", 0)
            sign = "+" if realized >= 0 else ""
            lines.append(f"  {wins}W / {losses}L | Win-Rate: {wr} | Realized: {sign}${realized:.2f}")

        # Beste Session
        best_session = max(td["by_session"].items(), key=lambda x: len(x[1]), default=(None, []))
        if best_session[0]:
            lines.append(f"  Aktivste Session: {best_session[0].upper()} ({len(best_session[1])} Trades)")

    # Box-Qualität
    if boxes:
        lines.append(f"\n📦 Aktuelle Boxes")
        for asset, box in boxes.items():
            rng   = box.get("high", 0) - box.get("low", 0)
            thr   = BREAKOUT_THRESHOLD.get(asset, 0)
            ratio = f"{thr/rng*100:.0f}% der Range" if rng > 0 else "?"
            lines.append(f"  {asset}: Range ${rng:.4f} (Threshold {ratio})")

    # Auffälligkeiten & Optimierungshinweise
    if anomalies:
        lines.append(f"\n⚠️ Auffälligkeiten")
        for a in anomalies[:4]:  # max 4 damit Telegram nicht zu lang wird
            lines.append(f"  {a}")

    # Footer
    lines.append(f"\nNächste Session: Tokyo 02:00 Uhr")
    lines.append(f"Tipp: /qualitycheck für tiefe Analyse")

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()

    try:
        print("📊 Erstelle Nacht-Report...")
        report = format_report()
        print(report)
        send_telegram_message(report, parse_mode="")
        print("✅ Nacht-Report gesendet")
    except Exception as e:
        import traceback
        err = f"💥 APEX nightly_report.py ERROR: {e}"
        print(err)
        traceback.print_exc()
        try:
            send_telegram_message(err)
        except Exception:
            pass
        sys.exit(1)

    print("NO_REPLY")
