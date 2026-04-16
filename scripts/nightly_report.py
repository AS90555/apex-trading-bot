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

PROJECT_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR       = os.path.join(PROJECT_DIR, "data")
LOGS_DIR       = os.path.join(PROJECT_DIR, "logs")
HWM_FILE       = os.path.join(DATA_DIR, "high_water_mark.json")
PENDING_NOTES  = os.path.join(DATA_DIR, "pending_notes.jsonl")
DEEP_FLAG      = os.path.join(DATA_DIR, "deep_review_pending.flag")
HYPOTHESIS_LOG = "/root/.claude/projects/-root-apex-trading-bot/memory/hypothesis_log.md"

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
            if ratio > 0.9:
                hints.append(f"⚡ {asset}: BREAKOUT_THRESHOLD (${threshold}) ist {ratio:.0%} der Box-Range — zu eng, kaum Trades möglich")
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


# ─── Health-Check ─────────────────────────────────────────────────────────────

def _health_alerts(balance: float) -> list:
    """Gibt eine Liste von Alert-Strings zurück. Leer = alles nominal."""
    alerts = []

    # 1. Unverarbeitete Pending Notes
    if os.path.exists(PENDING_NOTES):
        try:
            with open(PENDING_NOTES) as f:
                n = sum(1 for line in f if line.strip())
            if n > 0:
                alerts.append(f"📝 {n} Pending Notes nicht verarbeitet → Claude-Session starten")
        except OSError:
            pass

    # 2. Stale Deep-Review-Flag (> 48h)
    if os.path.exists(DEEP_FLAG):
        try:
            age_h = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(DEEP_FLAG))).total_seconds() / 3600
            if age_h > 48:
                alerts.append(f"🧪 Deep Review Flag seit {age_h:.0f}h offen → Claude-Session starten")
        except OSError:
            pass

    # 3. Hypothesen-Deadlines < 14 Tage
    if os.path.exists(HYPOTHESIS_LOG):
        try:
            with open(HYPOTHESIS_LOG) as f:
                content = f.read()
            today = datetime.now().date()
            for d in re.findall(r"- \*\*Deadline:\*\* .*?(\d{4}-\d{2}-\d{2})", content):
                days_left = (datetime.strptime(d, "%Y-%m-%d").date() - today).days
                if days_left <= 0:
                    alerts.append(f"⚠️ Hypothesen-Deadline ÜBERSCHRITTEN ({d})")
                elif days_left <= 14:
                    alerts.append(f"⏰ Hypothesen-Deadline in {days_left}d ({d})")
        except (OSError, re.error):
            pass

    # 4. Drawdown > 30%
    if balance > 0 and os.path.exists(HWM_FILE):
        try:
            hwm_data = load_json(HWM_FILE, {})
            hwm = hwm_data.get("hwm", 0)
            if hwm > 0:
                dd = ((hwm - balance) / hwm) * 100
                if dd > 30:
                    alerts.append(f"🔴 Drawdown {dd:.1f}% (${balance:.2f} vs HWM ${hwm:.2f})")
        except Exception:
            pass

    return alerts


# ─── Report formatieren ────────────────────────────────────────────────────────

def format_report():
    # Logs einlesen
    tokyo = analyse_session_log("tokyo.log", "Tokyo");  tokyo["session_key"] = "tokyo"
    eu    = analyse_session_log("eu.log",    "EU");     eu["session_key"]    = "eu"
    us    = analyse_session_log("us.log",    "US");     us["session_key"]    = "us"

    # Trade-Daten
    td = analyse_trades(days=7)

    # Aktuelle Boxes
    boxes = load_json(os.path.join(DATA_DIR, "opening_range_boxes.json"), {})

    # Balance direkt von Bitget holen (authoritative)
    balance_val = 0.0
    balance_txt = "unbekannt"
    hwm_txt = ""
    try:
        sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
        from bitget_client import BitgetClient
        sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
        try:
            from bot_config import DRY_RUN
        except ImportError:
            DRY_RUN = True
        client_r = BitgetClient(dry_run=DRY_RUN)
        balance_val = client_r.get_balance()
        balance_txt = f"${balance_val:,.2f} USDT"
    except Exception as e:
        print(f"⚠️  Balance-Fetch fehlgeschlagen: {e}")
        # Fallback: aus Daily-Log
        daily_lines = tail_log("daily.log", 50)
        for line in reversed(daily_lines):
            m = re.search(r"Balance:.*?\$([\d.,]+)", line)
            if m:
                balance_txt = f"${m.group(1)} USDT"
                balance_val = float(m.group(1).replace(",", ""))
                break
    hwm = load_json(HWM_FILE, {})
    if hwm.get("hwm"):
        hwm_txt = f" | HWM: ${hwm['hwm']:.2f}"

    # PnL direkt aus capital_tracking berechnen
    pnl_line = ""
    try:
        sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
        capital_file = os.path.join(DATA_DIR, "capital_tracking.json")
        cap = load_json(capital_file, {})
        start = cap.get("adjusted_start_capital") or cap.get("start_capital") or CAPITAL
        if balance_val > 0 and start > 0:
            pnl = balance_val - start
            pnl_pct = (pnl / start) * 100
            sign = "+" if pnl >= 0 else ""
            icon = "📈" if pnl >= 0 else "📉"
            pnl_line = f"{icon} Gesamt P&L: {sign}${pnl:.2f} ({sign}{pnl_pct:.2f}%) | Start: ${start:.2f}"
    except Exception:
        pass

    # Anomalien
    anomalies = detect_anomalies([tokyo, eu, us], td, boxes)

    # ── Report zusammenbauen ──
    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    day_str = f"{weekdays[TODAY.weekday()]} {TODAY.strftime('%d.%m.%Y')}"

    # ── Sessions zusammenfassen ──
    session_parts = []
    for s in [tokyo, eu, us]:
        name_short = s["name"].split()[0]  # "Tokyo", "EU", "US"
        if s["traded"]:
            session_parts.append(f"{name_short} getradet")
        elif s["breakouts"]:
            session_parts.append(f"{name_short} kein Entry")
        elif s["no_signal"]:
            session_parts.append(f"{name_short} kein Signal")
        else:
            session_parts.append(f"{name_short} keine Daten")
    sessions_line = "  ·  ".join(session_parts)

    # ── Heutige Trades ──
    trade_lines = []
    if td["today"]:
        for t in td["today"]:
            asset   = t.get("asset", "?")
            side    = "Long" if t.get("direction") == "long" else "Short"
            entry   = t.get("entry_price", 0)
            exit_p  = t.get("exit_price")
            pnl_r   = t.get("exit_pnl_r")
            pnl_usd = t.get("exit_pnl_usd")
            if exit_p and pnl_r is not None and pnl_usd is not None:
                sign = "+" if pnl_usd >= 0 else ""
                result = "Win" if pnl_usd > 0 else ("Loss" if pnl_usd < 0 else "BE")
                trade_lines.append(f"{asset} {side} — {result}  {sign}${pnl_usd:.2f} ({sign}{pnl_r}R)  Entry ${entry:,.4f} · Exit ${exit_p:,.4f}")
            else:
                trade_lines.append(f"{asset} {side} @ ${entry:,.4f} — läuft noch")

    # ── 7-Tage-Stats ──
    stats_line = ""
    if td["recent"]:
        tracker = td["pnl_tracker"]
        if tracker:
            wins     = tracker.get("winning_trades", 0)
            losses   = tracker.get("losing_trades", 0)
            total    = tracker.get("total_trades", 0)
            wr       = f"{wins/total*100:.0f}%" if total > 0 else "?"
            realized = tracker.get("realized_pnl", 0)
            sign     = "+" if realized >= 0 else ""
            stats_line = f"7 Tage: {wins}W / {losses}L  ·  {wr} WR  ·  {sign}${realized:.2f}"

    # ── Alerts & Anomalien ──
    alerts = _health_alerts(balance_val)
    alert_lines = [a for a in alerts[:3]]
    anomaly_lines = [a for a in (anomalies or [])[:2]]

    # ── Nachricht bauen ──
    lines = [f"Tagesabschluss {day_str}  —  {sessions_line}", ""]

    if trade_lines:
        for tl in trade_lines:
            lines.append(tl)
        lines.append("")

    lines.append(f"Book {balance_txt}{hwm_txt}")
    if pnl_line:
        lines.append(f"   {pnl_line}")
    if stats_line:
        lines.append(f"   {stats_line}")

    if alert_lines:
        lines.append("")
        for a in alert_lines:
            lines.append(f"Achtung — {a}")

    if anomaly_lines:
        lines.append("")
        for a in anomaly_lines:
            lines.append(f"Hinweis — {a}")

    lines.append("")
    lines.append("Nächste Session: Tokyo 02:00 Uhr")

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()

    try:
        print("📊 Erstelle Nacht-Report...")
        report = format_report()
        print(report)
        send_telegram_message(report, parse_mode="Markdown")
        print("✅ Nacht-Report gesendet")
    except Exception as e:
        import traceback
        err = f"Bot-Fehler (nightly_report) — {e}"
        print(err)
        traceback.print_exc()
        try:
            send_telegram_message(err)
        except Exception:
            pass
        sys.exit(1)

    print("NO_REPLY")
