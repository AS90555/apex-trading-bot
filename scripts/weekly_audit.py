#!/usr/bin/env python3
"""
APEX - Wöchentliches Skip-Audit (Opt 4)
========================================
Aggregiert die letzten 7 Tage aus data/skip_log.jsonl, schreibt Report nach
memory/reviews/skip_audit_YYYY-MM-DD.md und sendet eine Kurz-Summary via Telegram.

Typischer Cron-Slot: Sonntag 23:00 Berlin.

Manuell:
    python3 scripts/weekly_audit.py                 # letzten 7 Tage
    python3 scripts/weekly_audit.py --days 14       # letzten 14 Tage
    python3 scripts/weekly_audit.py --no-telegram   # ohne Telegram-Versand
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

SKIP_LOG_FILE = PROJECT_DIR / "data" / "skip_log.jsonl"
TRADES_FILE = PROJECT_DIR / "data" / "trades.json"
MEMORY_REVIEWS_DIR = Path.home() / ".claude" / "projects" / "-root-apex-trading-bot" / "memory" / "reviews"


def read_skip_log(since: datetime) -> list:
    """Liest skip_log.jsonl und filtert auf Einträge ab `since`."""
    if not SKIP_LOG_FILE.exists():
        return []
    entries = []
    with SKIP_LOG_FILE.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e.get("ts", "")[:26])
                if ts >= since:
                    e["_dt"] = ts
                    entries.append(e)
            except (ValueError, json.JSONDecodeError):
                continue
    return entries


def read_trades_in_window(since: datetime) -> list:
    """Liest trades.json und filtert auf Entries im Fenster."""
    if not TRADES_FILE.exists():
        return []
    try:
        with TRADES_FILE.open("r") as f:
            trades = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for t in trades:
        ts_str = t.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str[:26])
            if ts >= since:
                out.append(t)
        except ValueError:
            continue
    return out


def aggregate(entries: list) -> dict:
    """Gruppiert Skips nach Reason, Session, Asset."""
    by_reason = Counter(e.get("reason", "?") for e in entries)
    by_session = Counter(e.get("session") or "?" for e in entries)
    by_asset = Counter(e.get("asset") or "?" for e in entries if e.get("asset"))

    reason_session = defaultdict(Counter)
    for e in entries:
        reason_session[e.get("reason", "?")][e.get("session") or "?"] += 1

    return {
        "total": len(entries),
        "by_reason": dict(by_reason.most_common()),
        "by_session": dict(by_session.most_common()),
        "by_asset": dict(by_asset.most_common()),
        "reason_x_session": {k: dict(v) for k, v in reason_session.items()},
    }


def summarize_trades(trades: list) -> dict:
    """Win-Rate + R-Summe + Exit-Reason-Verteilung."""
    closed = [t for t in trades if t.get("exit_timestamp")]
    wins = [t for t in closed if (t.get("exit_pnl_usd") or 0) > 0]
    losses = [t for t in closed if (t.get("exit_pnl_usd") or 0) <= 0]
    total_r = sum((t.get("exit_pnl_r") or 0) for t in closed)
    total_usd = sum((t.get("exit_pnl_usd") or 0) for t in closed)
    exit_reasons = Counter(t.get("exit_reason", "?") for t in closed)

    return {
        "n_total": len(trades),
        "n_closed": len(closed),
        "n_open": len(trades) - len(closed),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "winrate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_r": round(total_r, 2),
        "total_usd": round(total_usd, 2),
        "exit_reasons": dict(exit_reasons),
    }


def render_report(since: datetime, until: datetime, agg: dict, trades_summary: dict) -> str:
    """Markdown-Report."""
    lines = []
    lines.append(f"# Skip-Audit {since.date()} → {until.date()}")
    lines.append("")
    lines.append(f"**Generiert:** {until.isoformat(timespec='seconds')}")
    lines.append(f"**Zeitraum:** {(until - since).days} Tage")
    lines.append("")

    lines.append("## Trade-Outcome im Fenster")
    ts = trades_summary
    lines.append(f"- Trades gesamt: {ts['n_total']} ({ts['n_closed']} closed, {ts['n_open']} offen)")
    lines.append(f"- Win-Rate (closed): {ts['winrate']}% ({ts['n_wins']}W / {ts['n_losses']}L)")
    lines.append(f"- Summe R: {ts['total_r']:+} | USD: ${ts['total_usd']:+.2f}")
    if ts["exit_reasons"]:
        lines.append("- Exit-Gründe:")
        for r, c in ts["exit_reasons"].items():
            lines.append(f"  - `{r}`: {c}")
    lines.append("")

    lines.append("## Skip-Funnel")
    lines.append(f"**Total Skips:** {agg['total']}")
    lines.append("")
    lines.append("### Nach Grund")
    lines.append("| Grund | N | Anteil |")
    lines.append("|---|---:|---:|")
    for r, n in agg["by_reason"].items():
        pct = (n / agg["total"] * 100) if agg["total"] else 0
        lines.append(f"| `{r}` | {n} | {pct:.1f}% |")
    lines.append("")

    lines.append("### Nach Session")
    lines.append("| Session | N |")
    lines.append("|---|---:|")
    for s, n in agg["by_session"].items():
        lines.append(f"| {s} | {n} |")
    lines.append("")

    if agg["by_asset"]:
        lines.append("### Nach Asset (nur wo Asset bekannt)")
        lines.append("| Asset | N |")
        lines.append("|---|---:|")
        for a, n in agg["by_asset"].items():
            lines.append(f"| {a} | {n} |")
        lines.append("")

    # Benchmark Tracker Block
    lines.append("## Benchmark-Vergleich (All-Time)")
    try:
        import sys as _sys
        _sys.path.insert(0, str(__file__).rsplit("/", 1)[0])
        from benchmark_tracker import run as _bt_run, render as _bt_render
        _apex, _hodl, _rand = _bt_run(use_api=False)
        lines.append("```")
        lines.append(_bt_render(_apex, _hodl, _rand))
        lines.append("```")
    except Exception as _e:
        lines.append(f"*(Benchmark-Tracker Fehler: {_e})*")
    lines.append("")

    # Actionable Findings
    lines.append("## Auffälligkeiten")
    findings = []
    if agg["total"]:
        top_reason, top_n = next(iter(agg["by_reason"].items()))
        top_pct = top_n / agg["total"] * 100
        if top_pct > 50:
            findings.append(
                f"⚠️  `{top_reason}` dominiert mit {top_pct:.0f}% aller Skips – "
                f"Parameter zu aggressiv oder Marktbedingung nicht passend?"
            )
    if ts["n_closed"] and ts["winrate"] < 30:
        findings.append(
            f"⚠️  Win-Rate {ts['winrate']}% liegt unter 30% – "
            f"Filter-Audit empfohlen (EMA/Volume/H4)."
        )
    if ts["total_r"] < -3:
        findings.append(
            f"⚠️  R-Summe {ts['total_r']:+} über {(until - since).days} Tage – "
            f"Hypothesen-Review priorisieren."
        )
    if not findings:
        findings.append("Keine Auffälligkeiten im Fenster.")
    for f in findings:
        lines.append(f"- {f}")
    lines.append("")

    return "\n".join(lines)


def render_telegram_summary(since: datetime, until: datetime, agg: dict, ts: dict) -> str:
    days = (until - since).days
    top_reason = next(iter(agg["by_reason"].items()), ("–", 0))
    return (
        f"Skip-Audit {days}d: {agg['total']} Skips | "
        f"Top: {top_reason[0]} ({top_reason[1]}). "
        f"Trades: {ts['n_closed']} closed, WR {ts['winrate']}%, "
        f"R {ts['total_r']:+}. Report in memory/reviews/."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="Analyse-Fenster in Tagen")
    parser.add_argument("--no-telegram", action="store_true", help="Kein Telegram-Versand")
    parser.add_argument("--stdout", action="store_true", help="Report nur auf stdout ausgeben, nichts schreiben")
    args = parser.parse_args()

    until = datetime.now()
    since = until - timedelta(days=args.days)

    entries = read_skip_log(since)
    trades = read_trades_in_window(since)
    agg = aggregate(entries)
    ts = summarize_trades(trades)
    report = render_report(since, until, agg, ts)

    if args.stdout:
        print(report)
        return 0

    MEMORY_REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = MEMORY_REVIEWS_DIR / f"skip_audit_{until.strftime('%Y-%m-%d')}.md"
    out_file.write_text(report)
    print(f"✅ Report geschrieben: {out_file}")

    if not args.no_telegram:
        try:
            from telegram_sender import send_telegram_message
            send_telegram_message(render_telegram_summary(since, until, agg, ts))
        except Exception as e:
            print(f"⚠️  Telegram-Versand fehlgeschlagen: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
