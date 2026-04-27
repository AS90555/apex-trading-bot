#!/usr/bin/env python3
"""
Bot Health Check — VAA & KDT
Läuft 3× täglich, analysiert Logs der letzten ~9h, schickt Telegram-Report.
"""

import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.telegram_sender import send_telegram_message

LOG_DIR = Path(__file__).parent.parent / "logs"

BOTS = {
    "VAA": LOG_DIR / "vaa.log",
    "KDT": LOG_DIR / "kdt.log",
}

# Fenster: letzte 9 Stunden (3 Check-Intervalle)
WINDOW_HOURS = 9

CRITICAL_PATTERNS = [
    (r"Traceback", "CRASH"),
    (r"Exception|EXCEPTION", "EXCEPTION"),
    (r"FATAL|fatal", "FATAL"),
]
WARNING_PATTERNS = [
    (r"429.*Rate Limit", "429 Rate-Limit"),
    (r"Order fehlgeschlagen", "Order-Fehler"),
    (r"Retry.*failed|fehlgeschlagen.*Retry", "Retry exhausted"),
    (r"⚠️(?!.*DRY RUN aktiv)(?!.*429)(?!.*Rate Limit)", "Warning"),
]


def parse_log(path: Path, since: datetime) -> dict:
    result = {
        "runs": 0,
        "last_run": None,
        "criticals": [],
        "warnings": [],
        "rate_limits": 0,
        "ok": False,
    }

    if not path.exists():
        result["criticals"].append("LOG-DATEI FEHLT")
        return result

    lines = path.read_text(errors="replace").splitlines()
    window_lines = []
    current_ts = None

    # Zeitstempel-Extraktion aus Start-Zeilen
    ts_re = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    in_window = False
    for line in lines:
        m = ts_re.search(line)
        if m and "Start" in line:
            try:
                current_ts = datetime.strptime(m.group(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                in_window = current_ts >= since
                if in_window:
                    result["runs"] += 1
                    if result["last_run"] is None or current_ts > result["last_run"]:
                        result["last_run"] = current_ts
            except ValueError:
                pass
        if in_window:
            window_lines.append(line)

    for line in window_lines:
        for pattern, label in CRITICAL_PATTERNS:
            if re.search(pattern, line):
                result["criticals"].append(label)
        for pattern, label in WARNING_PATTERNS:
            if re.search(pattern, line):
                if label == "429 Rate-Limit":
                    result["rate_limits"] += 1
                else:
                    result["warnings"].append(label)

    # Deduplizieren
    result["criticals"] = list(dict.fromkeys(result["criticals"]))
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["ok"] = result["runs"] > 0 and not result["criticals"]
    return result


def format_report(results: dict, since: datetime, now: datetime) -> str:
    window_label = f"{since.strftime('%H:%M')}-{now.strftime('%H:%M')} UTC"
    lines = [f"APEX Health-Check | {now.strftime('%Y-%m-%d')} | {window_label}"]
    lines.append("─" * 38)

    any_critical = False
    for bot, r in results.items():
        status = "✅ OK" if r["ok"] else ("🔴 KRITISCH" if r["criticals"] else "🟡 WARNUNG")
        if r["criticals"]:
            any_critical = True

        last = r["last_run"].strftime("%H:%M UTC") if r["last_run"] else "—"
        lines.append(f"{status}  {bot}  |  {r['runs']} Runs  |  letzter: {last}")

        if r["rate_limits"]:
            lines.append(f"   ⚠️  Rate-Limits: {r['rate_limits']}×")
        for w in r["warnings"]:
            lines.append(f"   ⚠️  {w}")
        for c in r["criticals"]:
            lines.append(f"   🔴 {c}")

    lines.append("─" * 38)
    if any_critical:
        lines.append("🔴 HANDLUNGSBEDARF — Bot prüfen!")
    elif any(r["warnings"] or r["rate_limits"] for r in results.values()):
        lines.append("🟡 Läuft — Kleinigkeiten im Blick behalten.")
    else:
        lines.append("✅ Alles sauber.")

    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)

    results = {bot: parse_log(path, since) for bot, path in BOTS.items()}
    report = format_report(results, since, now)

    print(report)
    send_telegram_message(report)


if __name__ == "__main__":
    main()
