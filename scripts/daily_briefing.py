#!/usr/bin/env python3
"""
APEX Daily Hedge Fund Briefing
================================
Läuft täglich 07:00 UTC — Multi-Bot-Überblick für alle aktiven Strategien.

Cron:
  0 7 * * *   cd /root/apex-trading-bot && venv/bin/python scripts/daily_briefing.py >> logs/daily.log 2>&1
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from scripts.telegram_sender import send_telegram_message, format_event_tag
from scripts.bitget_client import BitgetClient

DATA_DIR = os.path.join(PROJECT_DIR, "data")
HWM_FILE = os.path.join(DATA_DIR, "high_water_mark.json")

try:
    from config.bot_config import (
        DRY_RUN, CAPITAL,
        VAA_DRY_RUN, VAA_ASSETS,
        KDT_DRY_RUN, KDT_ASSET, KDT_MAX_RISK_PCT,
    )
except ImportError:
    DRY_RUN = True
    CAPITAL = 68.33
    VAA_DRY_RUN = True
    VAA_ASSETS = []
    KDT_DRY_RUN = True
    KDT_ASSET = "ETH"
    KDT_MAX_RISK_PCT = 0.02


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _today_trades(trades: list) -> list:
    today = datetime.now(timezone.utc).date()
    out = []
    for t in trades:
        try:
            d = datetime.fromisoformat(t.get("timestamp", "")).date()
            if d == today:
                out.append(t)
        except Exception:
            pass
    return out


def _bot_kpis(trades: list) -> dict:
    """n, wins, losses, total_r aus einer Trade-Liste schätzen.
    R wird nur gemeldet wenn exit_r vorhanden (Position-Monitor hat sie berechnet).
    """
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "total_r": None, "wr": None}
    wins   = sum(1 for t in trades if t.get("exit_r", 0) > 0)
    losses = sum(1 for t in trades if t.get("exit_r", 0) < 0)
    rs     = [t["exit_r"] for t in trades if "exit_r" in t]
    total_r = round(sum(rs), 2) if rs else None
    wr = round(wins / n * 100) if n > 0 else None
    return {"n": n, "wins": wins, "losses": losses, "total_r": total_r, "wr": wr}


def _fmt_kpis(kpis: dict) -> str:
    n = kpis["n"]
    if n == 0:
        return "0 Trades  —  noch kein Signal"
    wr_txt = f"{kpis['wr']}% WR" if kpis["wr"] is not None else "WR unbekannt"
    r_txt  = f"{kpis['total_r']:+.1f}R" if kpis["total_r"] is not None else "R unbekannt"
    return f"{n}T  {r_txt}  {wr_txt}"


# ─── Bot-Abschnitte ───────────────────────────────────────────────────────────

def _orb_section(today_trades: list, all_trades: list) -> list:
    kpis     = _bot_kpis(all_trades)
    mode     = "DRY" if DRY_RUN else "LIVE"
    lines    = [f"ORB  [{mode}]  Gesamt: {kpis['n']}T"]
    if today_trades:
        for t in today_trades:
            asset  = t.get("asset", "?")
            side   = "Long" if t.get("direction") == "long" else "Short"
            sess   = t.get("session", "").capitalize()
            entry  = t.get("entry_price", 0)
            exit_r = t.get("exit_r")
            if exit_r is not None:
                icon = "✅" if exit_r > 0 else ("⛔" if exit_r < 0 else "➖")
                lines.append(f"  {icon} {sess} {asset} {side}  {exit_r:+.2f}R  @ ${entry:,.2f}")
            else:
                lines.append(f"  ⏳ {sess} {asset} {side}  läuft  @ ${entry:,.2f}")
    else:
        lines.append("  — kein Trade heute —")
    return lines


def _vaa_section(today_trades: list, all_trades: list, pending: list) -> list:
    kpis  = _bot_kpis(all_trades)
    mode  = "DRY" if VAA_DRY_RUN else "LIVE"
    lines = [f"VAA  [{mode}]  {_fmt_kpis(kpis)}"]
    if today_trades:
        for t in today_trades:
            asset  = t.get("asset", "?")
            entry  = t.get("entry_price", 0)
            exit_r = t.get("exit_r")
            if exit_r is not None:
                icon = "✅" if exit_r > 0 else ("⛔" if exit_r < 0 else "➖")
                lines.append(f"  {icon} {asset} Short  {exit_r:+.2f}R  @ ${entry:,.4f}")
            else:
                lines.append(f"  ⏳ {asset} Short  läuft  @ ${entry:,.4f}")
    else:
        lines.append("  — kein Trade heute —")
    if pending:
        assets_p = ", ".join(p.get("asset", "?") for p in pending)
        lines.append(f"  Pending: {len(pending)} Signal(e) — {assets_p}")
    return lines


def _kdt_section(today_trades: list, all_trades: list, pending: list) -> list:
    kpis  = _bot_kpis(all_trades)
    mode  = "DRY" if KDT_DRY_RUN else "LIVE"
    n_all = len(all_trades)
    lines = [f"KDT  [{mode}]  {_fmt_kpis(kpis)}"]
    if today_trades:
        for t in today_trades:
            entry  = t.get("entry_price", 0)
            exit_r = t.get("exit_r")
            if exit_r is not None:
                icon = "✅" if exit_r > 0 else ("⛔" if exit_r < 0 else "➖")
                lines.append(f"  {icon} ETH Short  {exit_r:+.2f}R  @ ${entry:,.2f}")
            else:
                lines.append(f"  ⏳ ETH Short  läuft  @ ${entry:,.2f}")
    else:
        lines.append("  — kein Trade heute —")
    if pending:
        p = pending[0]
        lines.append(f"  Pending: Sell-Stop @ ${p.get('stop_price', 0):,.2f}  "
                     f"SL ${p.get('sl', 0):,.2f}")
    # Forward-Testing-Fortschritt
    goal = 10
    pct  = min(n_all, goal)
    bar  = "█" * pct + "░" * (goal - pct)
    lines.append(f"  Forward-Test: {n_all}/{goal} [{bar}]  "
                 f"→ Live-Gate nach {goal} DRY-Signalen")
    return lines


# ─── Balance & HWM ────────────────────────────────────────────────────────────

def _get_balance() -> tuple[float, float, float]:
    """Gibt (balance, hwm, pnl_pct) zurück."""
    try:
        client  = BitgetClient(dry_run=True)   # nur lesen, nie Order
        balance = client.get_balance()
    except Exception:
        balance = 0.0
    hwm_data = _load(HWM_FILE, {})
    hwm      = hwm_data.get("hwm", 0.0)
    start    = CAPITAL
    pnl_pct  = ((balance - start) / start * 100) if start > 0 and balance > 0 else 0.0
    return balance, hwm, pnl_pct


# ─── Offene Positionen ────────────────────────────────────────────────────────

def _open_positions() -> list[str]:
    try:
        client    = BitgetClient(dry_run=True)
        positions = client.get_positions()
        lines = []
        for p in positions:
            if p.size > 0:
                side = "Long" if p.is_long else "Short"
                pnl  = getattr(p, "unrealized_pnl", None)
                pnl_txt = f"  PnL: ${pnl:+.2f}" if pnl is not None else ""
                lines.append(f"  {p.coin} {side}  {p.size}{pnl_txt}")
        return lines
    except Exception:
        return ["  — Positions-Abruf fehlgeschlagen —"]


# ─── Factory Guard ────────────────────────────────────────────────────────────

def _factory_status() -> list[str]:
    daily = _load(os.path.join(DATA_DIR, "daily_pnl.json"), {})
    api   = _load(os.path.join(DATA_DIR, "factory_api_rate.json"), {})
    lines = []
    dd_r  = daily.get("daily_r", 0.0)
    dd_icon = "✅" if dd_r > -2.5 else ("⚠️" if dd_r > -4.0 else "🔴")
    lines.append(f"  DD heute: {dd_r:+.2f}R  {dd_icon}  (Half@−2.5R  Kill@−4.0R)")
    calls = api.get("calls_today", 0)
    lines.append(f"  API heute: {calls} Calls")
    return lines


# ─── Report bauen ─────────────────────────────────────────────────────────────

def build_report() -> str:
    now     = datetime.now(timezone.utc)
    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    date_str = f"{weekdays[now.weekday()]} {now.strftime('%d.%m.%Y')}  {now.strftime('%H:%M')} UTC"

    # Daten laden
    orb_all   = _load(os.path.join(DATA_DIR, "trades.json"), [])
    vaa_all   = _load(os.path.join(DATA_DIR, "vaa_trades.json"), [])
    kdt_all   = _load(os.path.join(DATA_DIR, "kdt_trades.json"), [])
    vaa_pend  = _load(os.path.join(DATA_DIR, "vaa_pending.json"), [])
    kdt_pend  = _load(os.path.join(DATA_DIR, "kdt_pending.json"), [])

    orb_today = _today_trades(orb_all)
    vaa_today = _today_trades(vaa_all)
    kdt_today = _today_trades(kdt_all)

    balance, hwm, pnl_pct = _get_balance()
    sign  = "+" if pnl_pct >= 0 else ""
    trend = "📈" if pnl_pct >= 0 else "📉"

    # ── Aufbau ──
    sep   = "─" * 38
    parts = []

    # Header
    parts.append(f"APEX DAILY BRIEF — {date_str}")
    parts.append(sep)

    # Book
    if balance > 0:
        hwm_txt = f"  HWM: ${hwm:.2f}" if hwm > 0 else ""
        parts.append(f"Book:  ${balance:.2f} USDT{hwm_txt}")
        parts.append(f"       Start: ${CAPITAL:.2f}  {trend} {sign}{pnl_pct:.1f}%")
    else:
        parts.append("Book:  — Abruf fehlgeschlagen —")

    parts.append(sep)

    # Offene Positionen
    pos_lines = _open_positions()
    parts.append("Offene Positionen:")
    parts.extend(pos_lines if pos_lines else ["  — keine —"])
    parts.append("")

    # Bots — heute
    parts.append("Heute:")
    parts.extend(_orb_section(orb_today, orb_all))
    parts.append("")
    parts.extend(_vaa_section(vaa_today, vaa_all, vaa_pend))
    parts.append("")
    parts.extend(_kdt_section(kdt_today, kdt_all, kdt_pend))
    parts.append(sep)

    # Factory Guard
    parts.append("Factory Guard:")
    parts.extend(_factory_status())
    parts.append(sep)

    # Next Session
    parts.append("Nächste ORB-Session: EU 09:15 CEST (07:15 UTC)")

    return "\n".join(parts)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    tag    = format_event_tag("FACTORY", "BRIEF")
    report = build_report()
    full   = f"{tag}\n{report}"
    print(full)
    send_telegram_message(full)
    print("✅ Daily Briefing gesendet")


if __name__ == "__main__":
    main()
