#!/usr/bin/env python3
"""
Strategy Snapshot — Phase D.2

Erstellt am 1. jedes Monats (oder manuell) einen Snapshot der aktuellen Strategie:
  - Aktiver Filter-Stack (bot_config.py Flags)
  - Hypothesen-Status-Übersicht
  - Balance, Total-R, WR aus trades.json + pnl_tracker.json

Output: memory/snapshots/config_YYYY-MM.md

Verwendung: python3 strategy_snapshot.py [--force]
Integration: /ASS Schritt 0 — Monats-Erste-Session-Modul.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

SNAPSHOT_DIR = Path("/root/.claude/projects/-root-apex-trading-bot/memory/snapshots")
HYPOTHESIS_LOG = Path("/root/.claude/projects/-root-apex-trading-bot/memory/hypothesis_log.md")
TRADES_FILE = Path("/root/apex-trading-bot/data/trades.json")
PNL_FILE = Path("/root/apex-trading-bot/data/pnl_tracker.json")
CONFIG_FILE = Path("/root/apex-trading-bot/config/bot_config.py")


def load_config_flags() -> dict[str, str]:
    """Lese alle H0XX_*_ENABLED und wichtige Parameter aus bot_config.py."""
    flags = {}
    if not CONFIG_FILE.exists():
        return flags
    text = CONFIG_FILE.read_text()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split("#")[0].strip()
        if any(key.startswith(p) for p in ("H0", "MAX_RISK", "LEVERAGE", "CAPITAL", "ASSETS")):
            flags[key] = val
    return flags


def parse_hypotheses() -> list[dict]:
    """Extrahiere alle Hypothesen-Status aus hypothesis_log.md."""
    if not HYPOTHESIS_LOG.exists():
        return []
    entries = []
    current = {}
    for line in HYPOTHESIS_LOG.read_text().splitlines():
        m = re.match(r"^## (H-\d+) · .*? · (.+)$", line)
        if m:
            if current:
                entries.append(current)
            current = {"id": m.group(1), "label": m.group(2)[:50], "status": "?"}
        if current and line.strip().startswith("- **Status:**"):
            current["status"] = line.split("**Status:**")[-1].strip()
    if current:
        entries.append(current)
    return entries


def load_trade_stats() -> dict:
    if not TRADES_FILE.exists():
        return {}
    trades = json.loads(TRADES_FILE.read_text())
    closed = [t for t in trades if t.get("exit_pnl_r") is not None]
    if not closed:
        return {"n": 0}
    rs = [t["exit_pnl_r"] for t in closed]
    wins = sum(1 for r in rs if r > 0)
    return {
        "n": len(closed),
        "win_rate": round(wins / len(closed) * 100, 1),
        "avg_r": round(mean(rs), 3),
        "total_r": round(sum(rs), 2),
    }


def load_balance() -> float | None:
    if not PNL_FILE.exists():
        return None
    try:
        d = json.loads(PNL_FILE.read_text())
        return d.get("realized_pnl")
    except Exception:
        return None


def should_run(force: bool) -> bool:
    if force:
        return True
    # Nur am 1. des Monats
    return datetime.now().day == 1


def render_snapshot(now: datetime) -> str:
    month = now.strftime("%Y-%m")
    flags = load_config_flags()
    hypotheses = parse_hypotheses()
    stats = load_trade_stats()
    pnl = load_balance()

    lines = [
        f"# APEX Strategy Snapshot — {month}",
        f"*Erstellt: {now.strftime('%Y-%m-%d %H:%M')}*",
        "",
        "## Performance",
        f"- Trades (closed): {stats.get('n', '?')}",
        f"- Win-Rate: {stats.get('win_rate', '?')}%",
        f"- Avg R: {stats.get('avg_r', '?')}R",
        f"- Total R: {stats.get('total_r', '?')}R",
        f"- Realized P&L: {'${:.2f}'.format(pnl) if pnl is not None else '?'}",
        "",
        "## Aktiver Filter-Stack (bot_config.py)",
    ]
    for k, v in sorted(flags.items()):
        lines.append(f"- `{k} = {v}`")

    lines += ["", "## Hypothesen-Status"]
    status_order = ["live", "verified", "shadow", "open", "inconclusive", "rejected"]
    def sort_key(h):
        s = h["status"].lower()
        for i, k in enumerate(status_order):
            if k in s:
                return i
        return 99
    for h in sorted(hypotheses, key=sort_key):
        lines.append(f"- **{h['id']}** {h['label'][:40]} — `{h['status']}`")

    lines += ["", "---", "*Nächster Snapshot: 1. des Folgemonats via /ASS Monats-Modul*"]
    return "\n".join(lines)


def main() -> int:
    force = "--force" in sys.argv
    now = datetime.now()
    if not should_run(force):
        print(f"Kein Snapshot-Tag (Tag {now.day}). Mit --force erzwingen.")
        return 0
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    month = now.strftime("%Y-%m")
    out_file = SNAPSHOT_DIR / f"config_{month}.md"
    if out_file.exists() and not force:
        print(f"Snapshot {out_file.name} existiert bereits. Mit --force überschreiben.")
        return 0
    content = render_snapshot(now)
    out_file.write_text(content)
    print(f"Snapshot geschrieben: {out_file}")
    print()
    print(content[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
