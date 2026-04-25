#!/usr/bin/env python3
"""
APEX Session Context — Läuft automatisch beim Session-Start via Hook.

Gibt einen strukturierten Kontext-Report aus den Claude im ersten Prompt liest.
Deckt alle 5 Session-Start-Pflichten aus CLAUDE.md ab:
  1. Bot-Status (alle Bots, Positionen, P&L)
  2. Pending Trade-Notes (unverarbeitete Exits)
  3. Deep Review Status
  4. Hypothesen-Deadlines / Gate-Nähe
  5. Log-Anomalien (Errors der letzten 24h)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

DATA_DIR   = os.path.join(PROJECT_DIR, "data")
MEMORY_DIR = os.path.join(PROJECT_DIR, "..", ".claude", "projects",
                          "-root-apex-trading-bot", "memory")
LOGS_DIR   = os.path.join(PROJECT_DIR, "logs")


def _section(title: str):
    print(f"\n┌─ {title} {'─' * max(0, 54 - len(title))}")


def _line(text: str):
    print(f"│  {text}")


def _end():
    print("└" + "─" * 58)


# ── 1. Bot-Status ─────────────────────────────────────────────────────────────

def section_bot_status():
    _section("BOT STATUS")
    try:
        import subprocess
        result = subprocess.run(
            [os.path.join(PROJECT_DIR, "venv", "bin", "python3"),
             os.path.join(PROJECT_DIR, "scripts", "bot_status.py")],
            capture_output=True, text=True, cwd=PROJECT_DIR
        )
        for line in result.stdout.splitlines():
            # Credentials-Zeilen unterdrücken
            if "Credentials" in line or "BitgetClient" in line:
                continue
            print(line)
    except Exception as e:
        _line(f"⚠️  bot_status.py Fehler: {e}")
    _end()


# ── 2. Pending Trade-Notes ─────────────────────────────────────────────────────

def section_pending_notes() -> int:
    notes_file = os.path.join(DATA_DIR, "pending_notes.jsonl")
    if not os.path.exists(notes_file):
        return 0

    notes = []
    try:
        with open(notes_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    notes.append(json.loads(line))
    except Exception:
        return 0

    if not notes:
        return 0

    _section(f"PENDING TRADE-NOTES  ({len(notes)} unverarbeitet)")
    for n in notes:
        ts      = n.get("ts", "?")[:16].replace("T", " ")
        asset   = n.get("asset", "?")
        session = n.get("session", "?").upper()
        pnl_r   = n.get("pnl_r", 0)
        reason  = n.get("exit_reason", "?")
        vol     = n.get("volume_ratio", 0)
        slip    = n.get("slippage_usd", 0)
        sign    = "+" if pnl_r >= 0 else ""
        _line(f"{ts}  {asset:<6} {session:<6} {sign}{pnl_r:.2f}R  "
              f"[{reason}]  Vol={vol:.1f}x  Slip=${slip:.2f}")
    _line("")
    _line("→ AKTION: Jede Note als Micro-Analyse in memory/trade_log.md schreiben,")
    _line("          dann pending_notes.jsonl leeren.")
    _end()
    return len(notes)


# ── 3. Deep Review Status ──────────────────────────────────────────────────────

def section_deep_review() -> bool:
    flag_file    = os.path.join(DATA_DIR, "deep_review_pending.flag")
    tracker_file = os.path.join(DATA_DIR, "pnl_tracker.json")

    flag_exists = os.path.exists(flag_file)
    trades_since = 0

    try:
        with open(tracker_file) as f:
            tracker = json.load(f)
        trades_since = tracker.get("trades_since_last_review", 0)
    except Exception:
        pass

    needs_review = flag_exists or trades_since >= 10

    _section("DEEP REVIEW STATUS")
    if needs_review:
        _line(f"🔴 DEEP REVIEW FÄLLIG")
        _line(f"   Trades seit letztem Review: {trades_since}")
        if flag_exists:
            _line(f"   Flag-Datei gesetzt: deep_review_pending.flag")
        _line("")
        _line("→ AKTION: Deep Review durchführen (letzte 10 Trades: WR, Avg R,")
        _line("          PF, Hypothesen-Gates). Report → memory/reviews/.")
    else:
        _line(f"✅ Kein Review fällig  (Trades seit letztem: {trades_since}/10)")
    _end()
    return needs_review


# ── 4. Hypothesen-Deadlines ────────────────────────────────────────────────────

def section_hypotheses():
    hyp_file = os.path.join(MEMORY_DIR, "hypothesis_log.md")
    if not os.path.exists(hyp_file):
        return

    now      = datetime.now(timezone.utc).date()
    urgent   = []   # Deadline < 14 Tage
    validating = [] # Status: validating oder live

    try:
        with open(hyp_file) as f:
            content = f.read()
    except Exception:
        return

    # Blöcke pro Hypothese (grob parsen)
    blocks = re.split(r'\n(?=## H-)', content)
    for block in blocks:
        id_match = re.search(r'## (H-\d+)', block)
        if not id_match:
            continue
        h_id = id_match.group(1)

        title_match = re.search(r'## H-\d+ · \d{4}-\d{2}-\d{2} · (.+)', block)
        title = title_match.group(1).strip() if title_match else "?"

        status_match = re.search(r'\*\*Status:\*\*\s*(.+)', block)
        status = status_match.group(1).strip() if status_match else ""

        deadline_match = re.search(r'\*\*Deadline:\*\*\s*(\d{4}-\d{2}-\d{2})', block)
        if deadline_match:
            try:
                dl = datetime.strptime(deadline_match.group(1), "%Y-%m-%d").date()
                days_left = (dl - now).days
                if days_left <= 14:
                    urgent.append((h_id, title, status, dl, days_left))
            except Exception:
                pass

        if any(kw in status.lower() for kw in ["validating", "live / validating"]):
            if not any(h[0] == h_id for h in urgent):
                validating.append((h_id, title, status))

    _section("HYPOTHESEN")

    if urgent:
        _line("🔴 Deadline in ≤ 14 Tagen:")
        for h_id, title, status, dl, days in urgent:
            suffix = "ABGELAUFEN" if days < 0 else f"noch {days}d"
            _line(f"   {h_id}: {title[:40]}  [{suffix}]")

    if validating:
        _line("🟡 Aktiv validating (kein Gate-Checkpoint verpassen):")
        for h_id, title, status in validating[:5]:
            _line(f"   {h_id}: {title[:40]}")

    if not urgent and not validating:
        _line("✅ Keine dringenden Hypothesen")

    _end()


# ── 5. Factory Guard ──────────────────────────────────────────────────────────

def section_factory_guard():
    _section("FACTORY GUARD")
    try:
        from scripts.factory_guard import FactoryGuard
        guard = FactoryGuard()

        dd  = guard.get_dd_status()
        api = guard.get_api_status()
        vaa = guard.check_vaa_live_gate()

        dd_icons  = {"OK": "✅", "HALF": "⚠️", "KILL": "🔴"}
        api_icons = {"OK": "✅", "WARN": "⚠️", "CRITICAL": "🔴"}

        _line(f"Daily DD  {dd_icons[dd['level']]}  {dd['total_r']:+.2f}R  "
              f"(Half@{dd['half_at']}R | Kill@{dd['kill_at']}R)")

        _line(f"API Rate  {api_icons[api['level']]}  "
              f"{api['calls_per_min']}/{api['limit_per_min']} Calls/min  "
              f"({api['ratio_pct']}%)  |  Heute: {api['total_today']}")

        vaa_icon = "✅" if vaa["gate_passed"] else "🟡"
        _line(f"VAA Gate  {vaa_icon}  {vaa['n_signals']}/{vaa['gate_target']} Signale  "
              f"{'→ BEREIT FÜR LIVE!' if vaa['ready_for_live'] else '→ noch nicht bereit'}")

        if dd["level"] == "KILL":
            _line("")
            _line("→ AKTION: 🔴 GLOBAL DD-KILL — alle Bots manuell prüfen!")
        elif dd["level"] == "HALF":
            _line("→ AKTION: ⚠️ Risk auf 50% — nächsten Trade mit halber Size")
        if api["level"] == "CRITICAL":
            _line("→ AKTION: 🔴 API-Limit — kurz warten vor nächstem Bot-Lauf")
        if vaa["ready_for_live"]:
            _line("→ AKTION: ✅ VAA Live-Gate bestanden — Freigabe mit Andre besprechen")

    except Exception as e:
        _line(f"⚠️  Factory Guard Fehler: {e}")
    _end()


# ── 6. Log-Anomalien ──────────────────────────────────────────────────────────

def section_log_anomalies():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    errors = []

    log_files = ["tokyo.log", "eu.log", "us.log", "monitor.log", "daily.log"]
    for fname in log_files:
        fpath = os.path.join(LOGS_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath) as f:
                lines = f.readlines()
            for line in lines[-200:]:  # nur letzte 200 Zeilen
                if any(kw in line for kw in ["ERROR", "FAILED", "Exception",
                                              "fehlgeschlagen", "Traceback"]):
                    errors.append((fname, line.rstrip()))
        except Exception:
            pass

    if not errors:
        return

    _section(f"LOG-ANOMALIEN  ({len(errors)} in letzten 24h)")
    shown = errors[-8:]  # max 8 zeigen
    for fname, line in shown:
        _line(f"[{fname}] {line[:80]}")
    if len(errors) > 8:
        _line(f"  … und {len(errors) - 8} weitere")
    _line("")
    _line("→ AKTION: Fehler analysieren falls sie Trading-Entscheidungen betreffen.")
    _end()


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'━'*60}")
    print(f"  APEX QUANT FACTORY — Session Context  [{now}]")
    print(f"{'━'*60}")

    section_bot_status()
    n_notes   = section_pending_notes()
    needs_rev = section_deep_review()
    section_hypotheses()
    section_factory_guard()
    section_log_anomalies()

    # Zusammenfassung + Commands-Cheatsheet
    print(f"\n{'━'*60}")
    print("  PFLICHTEN DIESE SESSION:")
    if n_notes > 0:
        print(f"  🔴 {n_notes} Trade-Note(s) in trade_log.md schreiben + Datei leeren")
    if needs_rev:
        print(f"  🔴 Deep Review durchführen")
    print(f"  ⚡ Höchste-Impact-Optimierung identifizieren & Andre präsentieren")

    print(f"\n{'━'*60}")
    print("  COMMANDS:")
    print("  /Status   → Bot-Dashboard (alle Bots, Positionen, P&L)")
    print("  /ASS      → Deep Analysis (Regime, Hypothesen, Filter, Attribution)")
    print("  /ASE      → Session End (Memory, CLAUDE.md, Git)")
    print("  /Lab [X]  → Neue Strategie evaluieren (Machbarkeit + Bauplan)")
    print("  /Build [X]→ Strategie vollständig validieren (Phase 0–6)")
    print("  /Review   → Deep Review (10-Trade Checkpoint)")
    print("  /asa      → Struktur-Audit (alle 25 Trades)")
    print("  /params   → Parameter-Kalibrierung (ORB Thresholds)")
    print("  /Panic    → Kill Switch (Bots stoppen + Positionen schließen)")
    print(f"{'━'*60}\n")


if __name__ == "__main__":
    main()
