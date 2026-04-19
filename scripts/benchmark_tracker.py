#!/usr/bin/env python3
"""
Benchmark Tracker — Phase E.1

Beantwortet: Schlägt APEX BTC-Hodl und einen Random-Entry-Bot?

Benchmarks:
  1. BTC Hodl     — $CAPITAL in BTC zum Zeitpunkt des ersten Trades gehalten
  2. Random Entry — gleiche Assets, gleiche Zeitpunkte, zufällige Richtung,
                    gleiche SL/TP-Logik (50% TP1@1R, TP2@3R, SL@1R)
                    → EV = 0 per Trade, zeigt ob APEX besser als Zufall ist

Output: Wöchentlich in weekly_audit; standalone mit python3 benchmark_tracker.py

Verwendung: python3 benchmark_tracker.py [--json]
Integration: weekly_audit.py (Block am Ende), /ASS Monats-Modul.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bitget_client import BitgetClient  # type: ignore  # noqa: E402

TRADES_FILE = Path("/root/apex-trading-bot/data/trades.json")
BENCHMARK_STATE = Path("/root/apex-trading-bot/data/benchmark_state.json")
STARTING_CAPITAL = 68.33  # USDT — Kapital beim ersten Trade


def load_closed_trades() -> list[dict]:
    return [t for t in json.loads(TRADES_FILE.read_text())
            if t.get("exit_pnl_r") is not None]


def load_state() -> dict:
    if BENCHMARK_STATE.exists():
        try:
            return json.loads(BENCHMARK_STATE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = BENCHMARK_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(BENCHMARK_STATE)


# ── BTC Hodl ──────────────────────────────────────────────────────────────────

def get_btc_hodl_pnl(client: BitgetClient, trades: list[dict]) -> dict:
    """
    Hypothetisch: beim ersten Trade-Zeitpunkt BTC gekauft, heute verkauft.
    Nutzt BTC-Preis aus erster Trade-Timestamp via API (oder gespeichertem Wert).
    """
    if not trades:
        return {"pnl_usd": 0.0, "pct": 0.0, "btc_entry": None, "btc_now": None}
    state = load_state()
    btc_entry = state.get("btc_hodl_entry_price")
    if not btc_entry:
        # Hole historischen BTC-Preis: nimm nächstliegenden Close aus 1d-Candle
        try:
            candles = client.get_candles("BTC", interval="1d", limit=60)
            first_ts_ms = int(trades[0].get("timestamp_ms") or
                              datetime.fromisoformat(
                                  trades[0]["timestamp"].replace("Z", "")
                              ).timestamp() * 1000)
            for c in candles:
                if c["time"] >= first_ts_ms:
                    btc_entry = c["open"]
                    break
            if not btc_entry and candles:
                btc_entry = candles[0]["close"]
        except Exception:
            return {"pnl_usd": None, "error": "BTC-Preis-Fetch fehlgeschlagen"}
        state["btc_hodl_entry_price"] = btc_entry
        state["btc_hodl_start_date"] = trades[0]["timestamp"][:10]
        save_state(state)
    btc_now = None
    try:
        btc_now = client.get_price("BTC")
    except Exception:
        pass
    if not btc_entry or not btc_now:
        return {"pnl_usd": None, "error": "Preis nicht verfügbar"}
    btc_qty = STARTING_CAPITAL / btc_entry
    pnl_usd = (btc_now - btc_entry) * btc_qty
    pct = (btc_now - btc_entry) / btc_entry * 100
    return {
        "pnl_usd": round(pnl_usd, 2),
        "pct": round(pct, 2),
        "btc_entry": round(btc_entry, 2),
        "btc_now": round(btc_now, 2),
        "start_date": state.get("btc_hodl_start_date", "?"),
    }


# ── Random Entry ──────────────────────────────────────────────────────────────

def simulate_random_entry(trades: list[dict], seed: int = 42) -> dict:
    """
    Simuliere Random-Entry: gleiche Trade-Zeitpunkte, zufällige Long/Short-Entscheidung.
    Verwendet gleiche exit_pnl_r wie echter Trade — aber mit invertiertem Vorzeichen
    wenn Richtung anders gewählt wurde.

    Vereinfachung: Random-Bot trifft exakt gleiche Asset-Entscheidung wie APEX,
    nur Richtung ist zufällig. Das misst ob APEX-Richtungsauswahl Edge hat.
    """
    if not trades:
        return {"pnl_r": 0.0, "n": 0}
    rng = random.Random(seed)
    total_r = 0.0
    wins = 0
    for t in trades:
        real_dir = t.get("direction", "long")
        rand_dir = rng.choice(["long", "short"])
        r = t["exit_pnl_r"]
        # Wenn Random-Richtung gleich wie APEX → gleiches Ergebnis
        # Wenn Random-Richtung anders → invertiertes Ergebnis
        simulated_r = r if rand_dir == real_dir else -r
        total_r += simulated_r
        if simulated_r > 0:
            wins += 1
    n = len(trades)
    return {
        "pnl_r": round(total_r, 2),
        "pnl_usd": round(total_r * (STARTING_CAPITAL * 0.02), 2),
        "win_rate": round(wins / n * 100, 1),
        "avg_r": round(total_r / n, 3),
        "n": n,
    }


# ── APEX Actual ───────────────────────────────────────────────────────────────

def apex_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"pnl_r": 0.0, "n": 0}
    rs = [t["exit_pnl_r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    total_r = sum(rs)
    # $-P&L aus risk_usd wenn vorhanden, sonst approximieren
    pnl_usd = sum(t.get("risk_usd", STARTING_CAPITAL * 0.02) * t["exit_pnl_r"]
                  for t in trades)
    return {
        "pnl_r": round(total_r, 2),
        "pnl_usd": round(pnl_usd, 2),
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_r": round(total_r / len(trades), 3),
        "n": len(trades),
    }


# ── Render ────────────────────────────────────────────────────────────────────

def render(apex: dict, hodl: dict, rand: dict) -> str:
    def tag(apex_val, bench_val, higher_better=True):
        if bench_val is None:
            return ""
        if higher_better:
            return " ✅ besser" if apex_val > bench_val else " ❌ schlechter"
        return " ✅ besser" if apex_val < bench_val else " ❌ schlechter"

    lines = ["Benchmark Tracker — APEX vs. Markt", "=" * 52]
    n = apex.get("n", 0)
    lines.append(f"Zeitraum: {n} geschlossene Trades\n")

    # APEX
    lines.append(f"APEX:       {apex['pnl_r']:+.2f}R  ${apex['pnl_usd']:+.2f}  WR {apex['win_rate']:.0f}%")

    # BTC Hodl
    if hodl.get("pnl_usd") is not None:
        t = tag(apex["pnl_usd"], hodl["pnl_usd"])
        lines.append(f"BTC Hodl:   {'n/a':>8}  ${hodl['pnl_usd']:+.2f}  ({hodl['pct']:+.1f}%){t}")
        lines.append(f"  Entry ${hodl.get('btc_entry','?')} → Now ${hodl.get('btc_now','?')} (ab {hodl.get('start_date','?')})")
    else:
        lines.append(f"BTC Hodl:   — ({hodl.get('error', 'n/a')})")

    # Random Entry
    t = tag(apex["pnl_usd"], rand.get("pnl_usd"))
    lines.append(f"Random:     {rand['pnl_r']:+.2f}R  ${rand['pnl_usd']:+.2f}  WR {rand['win_rate']:.0f}%{t}")
    lines.append("")
    lines.append("Interpretation:")
    lines.append("  APEX besser als BTC Hodl → Bot schlägt passives Investment")
    lines.append("  APEX besser als Random  → Richtungsauswahl hat Edge")
    lines.append("  APEX schlechter als Random → Signal-Qualität prüfen (filters helfen nicht?)")
    return "\n".join(lines)


def run(use_api: bool = True) -> tuple[dict, dict, dict]:
    trades = load_closed_trades()
    apex = apex_stats(trades)
    rand = simulate_random_entry(trades)
    if use_api:
        try:
            client = BitgetClient(dry_run=True)
            hodl = get_btc_hodl_pnl(client, trades)
        except Exception as e:
            hodl = {"pnl_usd": None, "error": str(e)}
    else:
        hodl = {"pnl_usd": None, "error": "API disabled"}
    return apex, hodl, rand


def main() -> int:
    apex, hodl, rand = run()
    if "--json" in sys.argv:
        print(json.dumps({"apex": apex, "hodl": hodl, "random": rand}, indent=2))
    else:
        print(render(apex, hodl, rand))
    return 0


if __name__ == "__main__":
    sys.exit(main())
