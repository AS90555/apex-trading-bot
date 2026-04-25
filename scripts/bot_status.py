#!/usr/bin/env python3
"""
APEX Bot Status Dashboard — CLI-Übersicht aller aktiven Bots, P&L und Live-Positionen.

Usage:
  python scripts/bot_status.py                    # Normaler Status-Report
  python scripts/bot_status.py --kill             # PANIC BUTTON (fragt nach Bestätigung)
  python scripts/bot_status.py --kill --confirm   # PANIC BUTTON ohne Prompt (für Claude)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from config.bot_config import DATA_DIR

# Cron-Skripte die zum Trading gehören (werden bei --kill kommentiert)
TRADING_SCRIPTS = [
    "pre_market.py",
    "save_opening_range.py",
    "autonomous_trade.py",
    "weekend_momo.py",
    "position_monitor.py",
    "daily_closeout.py",
    "session_summary.py",
]

# Bekannte Bot-Skripte: automatisch aus data/*_trades.json + scripts/*_bot.py erkannt
BOT_SCRIPT_PATTERN = re.compile(r"(\w+)_bot\.py")

# Lesebare Namen für bekannte Bots
BOT_LABELS = {
    "trades":    "ORB",
    "vaa":       "VAA",
}


# ─── Auto-Detection ───────────────────────────────────────────────────────────

def discover_bots() -> list[dict]:
    """
    Findet alle Bots automatisch anhand von data/*_trades.json.
    Gibt Liste von {key, label, trades_file, pending_file, script} zurück.
    """
    pattern = os.path.join(DATA_DIR, "*_trades.json")
    files   = sorted(glob.glob(pattern))

    # Fallback: trades.json (ORB) hat kein Präfix
    orb_file = os.path.join(DATA_DIR, "trades.json")
    bots = []

    # ORB zuerst (Sonderfall: kein Präfix)
    if os.path.exists(orb_file):
        bots.append({
            "key":          "trades",
            "label":        BOT_LABELS.get("trades", "ORB"),
            "trades_file":  orb_file,
            "pending_file": None,
            "script":       "autonomous_trade.py",
        })

    for f in files:
        base = os.path.basename(f)                    # z.B. "vaa_trades.json"
        key  = base.replace("_trades.json", "")       # z.B. "vaa"
        if key == "trades":
            continue                                   # ORB schon oben
        label   = BOT_LABELS.get(key, key.upper())
        pending = os.path.join(DATA_DIR, f"{key}_pending.json")
        script  = f"{key}_bot.py"
        bots.append({
            "key":          key,
            "label":        label,
            "trades_file":  f,
            "pending_file": pending if os.path.exists(pending) else None,
            "script":       script,
        })

    return bots


def discover_trading_scripts() -> list[str]:
    """Gibt alle Trading-Skripte zurück (statische Liste + auto-detektierte *_bot.py)."""
    scripts_dir = os.path.join(PROJECT_DIR, "scripts")
    bot_scripts = [
        os.path.basename(f)
        for f in glob.glob(os.path.join(scripts_dir, "*_bot.py"))
    ]
    return list(set(TRADING_SCRIPTS + bot_scripts))


# ─── Daten-Laden ──────────────────────────────────────────────────────────────

def load_trades(path: str) -> list:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_pending(path: str | None) -> list:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


# ─── P&L-Berechnung ───────────────────────────────────────────────────────────

def calc_pnl(trades: list) -> dict:
    closed = [t for t in trades if t.get("exit_pnl_r") is not None]
    open_  = [t for t in trades if t.get("exit_pnl_r") is None]
    dry    = sum(1 for t in closed if t.get("dry_run"))

    if not closed:
        return {"n": 0, "open": len(open_), "dry": dry,
                "total_r": 0.0, "avg_r": 0.0, "wr": 0.0,
                "wins": 0, "losses": 0, "total_usd": 0.0, "max_dd_r": 0.0}

    r_vals   = [t["exit_pnl_r"] for t in closed]
    usd_vals = [t.get("exit_pnl_usd", 0) or 0 for t in closed]
    wins     = sum(1 for r in r_vals if r > 0)
    total_r  = sum(r_vals)

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in r_vals:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return {
        "n":         len(closed),
        "open":      len(open_),
        "dry":       dry,
        "wins":      wins,
        "losses":    len(r_vals) - wins,
        "total_r":   round(total_r, 2),
        "avg_r":     round(total_r / len(r_vals), 3),
        "wr":        round(wins / len(r_vals) * 100, 1),
        "total_usd": round(sum(usd_vals), 2),
        "max_dd_r":  round(max_dd, 2),
    }


# ─── Crontab-Parsing ──────────────────────────────────────────────────────────

def get_active_cron_bots(all_scripts: list[str]) -> set[str]:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines  = result.stdout.splitlines()
    except Exception:
        return set()

    active = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        for script in all_scripts:
            if script in stripped:
                active.add(script)
    return active


# ─── Live-Positionen ──────────────────────────────────────────────────────────

def get_live_positions():
    try:
        from scripts.bitget_client import BitgetClient
        client = BitgetClient(dry_run=False)
        return client.get_positions()
    except Exception as e:
        print(f"  ⚠️  Positions-API Fehler: {e}")
        return []


# ─── PANIC BUTTON ─────────────────────────────────────────────────────────────

def kill_all(confirmed: bool = False):
    print("\n" + "═" * 60)
    print("  🚨  PANIC BUTTON AKTIVIERT")
    print("═" * 60)

    if not confirmed:
        answer = input(
            "\n  Alle Bots stoppen + alle Positionen schließen?\n"
            "  Tippe 'JA' zum Bestätigen: "
        ).strip()
        if answer != "JA":
            print("  Abgebrochen.")
            return

    all_scripts = discover_trading_scripts()

    # Schritt 1: Crontab einfrieren
    print("\n[1/3] Crontab einfrieren ...")
    try:
        result   = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines    = result.stdout.splitlines(keepends=True)
        frozen   = []
        killed   = 0
        for line in lines:
            strip = line.strip()
            if not strip.startswith("#") and any(s in strip for s in all_scripts):
                frozen.append("# [KILLED] " + line)
                killed += 1
            else:
                frozen.append(line)
        proc = subprocess.run(["crontab", "-"], input="".join(frozen),
                               text=True, capture_output=True)
        if proc.returncode == 0:
            print(f"  ✅  {killed} Cron-Job(s) auskommentiert")
        else:
            print(f"  ❌  Crontab-Schreibfehler: {proc.stderr}")
    except Exception as e:
        print(f"  ❌  Crontab-Fehler: {e}")

    # Schritt 2: Alle offenen Positionen schließen
    print("\n[2/3] Offene Positionen schließen ...")
    try:
        from scripts.bitget_client import BitgetClient
        client    = BitgetClient(dry_run=False)
        positions = client.get_positions()
        live      = [p for p in positions if p.size > 0]
        if not live:
            print("  ✅  Keine offenen Positionen")
        else:
            for pos in live:
                try:
                    r = client.place_market_order(
                        coin=pos.coin, is_buy=(pos.direction != "long"),
                        size=pos.size, reduce_only=True,
                    )
                    status = "✅" if r.success else "❌"
                    print(f"  {status}  {pos.coin} {pos.direction.upper()} "
                          f"(size={pos.size})")
                except Exception as e:
                    print(f"  ❌  {pos.coin}: {e}")
    except Exception as e:
        print(f"  ❌  Positions-Close-Fehler: {e}")

    # Schritt 3: Alle Pending-Signale löschen
    print("\n[3/3] Pending-Signale löschen ...")
    pending_files = glob.glob(os.path.join(DATA_DIR, "*_pending.json"))
    if not pending_files:
        print("  ✅  Keine Pending-Files gefunden")
    for pf in pending_files:
        try:
            tmp = pf + ".tmp"
            with open(tmp, "w") as f:
                json.dump([], f)
            os.replace(tmp, pf)
            print(f"  ✅  {os.path.basename(pf)} geleert")
        except Exception as e:
            print(f"  ❌  {os.path.basename(pf)}: {e}")

    print("\n" + "═" * 60)
    print("  ⚠️  Crontab reaktivieren: crontab -e")
    print("     (# [KILLED] Zeilen entkommentieren)")
    print("═" * 60 + "\n")


# ─── Haupt-Report ─────────────────────────────────────────────────────────────

def print_status():
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bots     = discover_bots()
    all_scr  = discover_trading_scripts()
    active   = get_active_cron_bots(all_scr)

    print(f"\n{'═'*60}")
    print(f"  APEX Quant Factory  —  {now}")
    print(f"{'═'*60}")

    # ── Aktive Bots ──────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  BOTS")
    print(f"{'─'*60}")

    for bot in bots:
        is_on  = bot["script"] in active
        trades = load_trades(bot["trades_file"])
        pnl    = calc_pnl(trades)

        # DRY_RUN-Status aus letztem Trade lesen
        last_dry = None
        for t in reversed(trades):
            if "dry_run" in t:
                last_dry = t["dry_run"]
                break
        mode = "DRY" if last_dry else "LIVE" if last_dry is not None else "?"

        status_icon = "🟢" if is_on else "🔴"
        pnl_str = (f"{'+' if pnl['total_r'] >= 0 else ''}{pnl['total_r']:.1f}R"
                   if pnl["n"] > 0 else "—")
        wr_str  = f"WR {pnl['wr']:.0f}%" if pnl["n"] > 0 else ""
        n_str   = f"n={pnl['n']}" if pnl["n"] > 0 else "0 Trades"

        print(f"  {status_icon} {bot['label']:<8} [{mode}]  "
              f"{n_str:<10}  {pnl_str:<8}  {wr_str}")

    # ── Offene Positionen ────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  OFFENE POSITIONEN")
    print(f"{'─'*60}")
    positions = get_live_positions()
    live_pos  = [p for p in positions if p.size > 0]
    if not live_pos:
        print("  — keine —")
    else:
        for p in live_pos:
            pnl_str = (f"  PnL: ${p.unrealised_pnl:+.2f}"
                       if hasattr(p, "unrealised_pnl") else "")
            print(f"  {p.coin:<8} {p.direction.upper():<6}  size={p.size}{pnl_str}")

    # ── Pending-Signale ───────────────────────────────────────────────────────
    all_pending = []
    for bot in bots:
        pending = load_pending(bot["pending_file"])
        for sig in pending:
            sig["_bot"] = bot["label"]
            all_pending.append(sig)

    if all_pending:
        print(f"\n{'─'*60}")
        print(f"  PENDING SIGNALE  ({len(all_pending)})")
        print(f"{'─'*60}")
        for sig in all_pending:
            print(f"  [{sig['_bot']}] {sig.get('asset','?'):<8}  "
                  f"Stop@{sig.get('stop_price', sig.get('entry','?'))}  "
                  f"SL={sig.get('sl','?')}")

    # ── P&L Gesamt ───────────────────────────────────────────────────────────
    all_trades = []
    for bot in bots:
        all_trades.extend(load_trades(bot["trades_file"]))
    total = calc_pnl(all_trades)

    if total["n"] > 0:
        print(f"\n{'─'*60}")
        sign = "+" if total["total_r"] >= 0 else ""
        usd_sign = "+" if total["total_usd"] >= 0 else ""
        print(f"  GESAMT   {total['n']} Trades  |  "
              f"{sign}{total['total_r']:.2f}R  "
              f"(${usd_sign}{total['total_usd']:.2f})  |  "
              f"WR: {total['wr']:.0f}%  |  "
              f"MaxDD: -{total['max_dd_r']:.2f}R")

    # ── Factory Guard ─────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  FACTORY GUARD")
    print(f"{'─'*60}")
    try:
        from scripts.factory_guard import FactoryGuard
        guard = FactoryGuard()

        dd  = guard.get_dd_status()
        api = guard.get_api_status()
        vaa = guard.check_vaa_live_gate()

        dd_icons  = {"OK": "✅", "HALF": "⚠️ ", "KILL": "🔴"}
        api_icons = {"OK": "✅", "WARN": "⚠️ ", "CRITICAL": "🔴"}
        vaa_icon  = "✅" if vaa["gate_passed"] else "🟡"

        bar_filled = int(dd["total_r"] / dd["kill_at"] * 10) if dd["kill_at"] else 0
        bar_filled = max(0, min(10, abs(bar_filled)))
        bar = "█" * bar_filled + "░" * (10 - bar_filled)

        print(f"  Daily DD  {dd_icons[dd['level']]}  "
              f"{dd['total_r']:+.2f}R  [{bar}]  "
              f"Half@{dd['half_at']}R  Kill@{dd['kill_at']}R")
        if dd["bots"]:
            parts = "  ".join(f"{b}: {r:+.1f}R" for b, r in dd["bots"].items())
            print(f"             ↳ {parts}")

        print(f"  API Rate  {api_icons[api['level']]}  "
              f"{api['calls_per_min']}/{api['limit_per_min']} Calls/min  "
              f"({api['ratio_pct']}%)  |  Heute: {api['total_today']} Calls")

        print(f"  VAA Gate  {vaa_icon}  "
              f"{vaa['n_signals']}/{vaa['gate_target']} Signale  "
              f"Pending: {vaa['n_pending']}  "
              f"{'→ BEREIT FÜR LIVE ✅' if vaa['ready_for_live'] else '→ noch nicht bereit'}")
        if vaa["anomalies"]:
            for a in vaa["anomalies"]:
                print(f"             ⚠️  {a}")

    except Exception as e:
        print(f"  ⚠️  Factory Guard Fehler: {e}")

    print(f"{'═'*60}\n")


# ─── Einstieg ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="APEX Bot Status Dashboard")
    parser.add_argument("--kill",    action="store_true",
                        help="PANIC BUTTON: alle Bots stoppen + Positionen schließen")
    parser.add_argument("--confirm", action="store_true",
                        help="Bestätigung überspringen (für automatisierte Nutzung)")
    args = parser.parse_args()

    if args.kill:
        kill_all(confirmed=args.confirm)
    else:
        print_status()


if __name__ == "__main__":
    main()
