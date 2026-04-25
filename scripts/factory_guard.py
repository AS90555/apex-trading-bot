#!/usr/bin/env python3
"""
APEX Factory Guard — Gemeinsame Schutzmechanismen für alle Bots.

Drei Funktionen:
  1. Globales Daily-DD-Limit  — bots-übergreifend, verhindert Ruin bei Korrelation
  2. API-Rate-Monitor         — zählt Calls pro Minute, warnt vor Überlastung
  3. VAA Live-Gate Validator  — prüft ob VAA-DRY-RUN bereit für LIVE

Nutzung:
  from scripts.factory_guard import FactoryGuard
  guard = FactoryGuard()
  ok, reason = guard.check_daily_dd(bot_name="ORB", new_r=-1.0)
  if not ok:
      print(reason); sys.exit()
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from config.bot_config import DATA_DIR

DAILY_DD_FILE   = os.path.join(DATA_DIR, "factory_daily_dd.json")
API_RATE_FILE   = os.path.join(DATA_DIR, "factory_api_rate.json")

# ─── Globales Daily-DD-Limit ──────────────────────────────────────────────────
# Jeder Bot zählt 2% Risk. Bei 10 Bots = 20% Exposure.
# Globales Limit: -4R/Tag = ca. -8% Balance → danach alle Bots gesperrt.
GLOBAL_DAILY_DD_KILL_R  = -4.0   # Alle Bots stoppen
GLOBAL_DAILY_DD_HALF_R  = -2.5   # Alle Bots auf 50% Risk

# ─── API-Rate-Limit ───────────────────────────────────────────────────────────
# Bitget Private API: 10 Req/s pro Endpoint, praktisch ~20 Req/s gesamt.
# Wir nutzen das konservativ: max 60 Calls/Minute (1/s Durchschnitt).
# Bei 10 Bots und parallelen Scans: brauchen wir Puffer.
API_LIMIT_PER_MINUTE    = 60     # Warnung ab 80% = 48 Calls/min
API_WARN_THRESHOLD      = 0.80   # 80% → Warnung
API_CRITICAL_THRESHOLD  = 0.95   # 95% → Block


class FactoryGuard:

    # ── Daily DD ──────────────────────────────────────────────────────────────

    def _load_dd(self) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not os.path.exists(DAILY_DD_FILE):
            return {"date": today, "bots": {}, "total_r": 0.0}
        try:
            with open(DAILY_DD_FILE) as f:
                data = json.load(f)
            if data.get("date") != today:
                return {"date": today, "bots": {}, "total_r": 0.0}
            return data
        except Exception:
            return {"date": today, "bots": {}, "total_r": 0.0}

    def _save_dd(self, data: dict):
        tmp = DAILY_DD_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DAILY_DD_FILE)

    def record_trade_r(self, bot_name: str, r: float):
        """Registriert ein abgeschlossenes Trade-Ergebnis in R."""
        data = self._load_dd()
        data["bots"].setdefault(bot_name, 0.0)
        data["bots"][bot_name] = round(data["bots"][bot_name] + r, 4)
        data["total_r"] = round(sum(data["bots"].values()), 4)
        self._save_dd(data)

    def check_daily_dd(self, bot_name: str = None) -> tuple[bool, str, float]:
        """
        Prüft ob ein neuer Trade erlaubt ist.
        Gibt (allowed, reason, risk_modifier) zurück.
        risk_modifier = 1.0 (voll), 0.5 (halb), 0.0 (gesperrt)
        """
        data    = self._load_dd()
        total_r = data.get("total_r", 0.0)

        if total_r <= GLOBAL_DAILY_DD_KILL_R:
            return False, (
                f"🔴 GLOBAL DD-KILL: {total_r:.1f}R ≤ {GLOBAL_DAILY_DD_KILL_R}R "
                f"— alle Bots gestoppt bis Mitternacht"
            ), 0.0

        if total_r <= GLOBAL_DAILY_DD_HALF_R:
            return True, (
                f"⚠️ GLOBAL DD-HALF: {total_r:.1f}R ≤ {GLOBAL_DAILY_DD_HALF_R}R "
                f"— Risk auf 50% reduziert"
            ), 0.5

        return True, f"✅ Global DD OK: {total_r:.2f}R", 1.0

    def get_dd_status(self) -> dict:
        """Gibt aktuellen DD-Status als Dict zurück (für Dashboards)."""
        data    = self._load_dd()
        total_r = data.get("total_r", 0.0)

        if total_r <= GLOBAL_DAILY_DD_KILL_R:
            level = "KILL"
        elif total_r <= GLOBAL_DAILY_DD_HALF_R:
            level = "HALF"
        else:
            level = "OK"

        return {
            "date":    data.get("date"),
            "total_r": total_r,
            "bots":    data.get("bots", {}),
            "level":   level,
            "kill_at": GLOBAL_DAILY_DD_KILL_R,
            "half_at": GLOBAL_DAILY_DD_HALF_R,
        }

    # ── API-Rate-Monitor ──────────────────────────────────────────────────────

    def _load_rate(self) -> dict:
        now = time.time()
        if not os.path.exists(API_RATE_FILE):
            return {"calls": [], "total_today": 0}
        try:
            with open(API_RATE_FILE) as f:
                data = json.load(f)
            # Nur Calls der letzten 60 Sekunden behalten
            data["calls"] = [t for t in data.get("calls", []) if now - t < 60]
            return data
        except Exception:
            return {"calls": [], "total_today": 0}

    def _save_rate(self, data: dict):
        tmp = API_RATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, API_RATE_FILE)

    def record_api_call(self):
        """Registriert einen API-Call. Wird von bitget_client aufgerufen."""
        data = self._load_rate()
        data["calls"].append(time.time())
        data["total_today"] = data.get("total_today", 0) + 1
        self._save_rate(data)

    def check_api_rate(self) -> tuple[bool, str, int]:
        """
        Prüft ob API-Call erlaubt ist.
        Gibt (allowed, reason, calls_per_minute) zurück.
        """
        data  = self._load_rate()
        calls = len(data.get("calls", []))   # Calls in letzten 60s
        ratio = calls / API_LIMIT_PER_MINUTE

        if ratio >= API_CRITICAL_THRESHOLD:
            return False, (
                f"🔴 API CRITICAL: {calls}/{API_LIMIT_PER_MINUTE} Calls/min "
                f"— kurz warten"
            ), calls

        if ratio >= API_WARN_THRESHOLD:
            return True, (
                f"⚠️ API WARN: {calls}/{API_LIMIT_PER_MINUTE} Calls/min "
                f"({ratio*100:.0f}%)"
            ), calls

        return True, f"✅ API OK: {calls}/{API_LIMIT_PER_MINUTE} Calls/min", calls

    def get_api_status(self) -> dict:
        """Gibt aktuellen API-Status als Dict zurück."""
        data    = self._load_rate()
        calls   = len(data.get("calls", []))
        ratio   = calls / API_LIMIT_PER_MINUTE
        total   = data.get("total_today", 0)

        if ratio >= API_CRITICAL_THRESHOLD:
            level = "CRITICAL"
        elif ratio >= API_WARN_THRESHOLD:
            level = "WARN"
        else:
            level = "OK"

        return {
            "calls_per_min":  calls,
            "limit_per_min":  API_LIMIT_PER_MINUTE,
            "ratio_pct":      round(ratio * 100, 1),
            "total_today":    total,
            "level":          level,
        }

    # ── VAA Live-Gate Validator ───────────────────────────────────────────────

    def check_vaa_live_gate(self) -> dict:
        """
        Prüft ob VAA bereit ist für LIVE.
        Gate: ≥10 DRY-RUN-Signale ohne kritische Anomalie.
        """
        vaa_trades_file  = os.path.join(DATA_DIR, "vaa_trades.json")
        vaa_pending_file = os.path.join(DATA_DIR, "vaa_pending.json")

        # Abgeschlossene DRY-RUN-Trades
        trades = []
        if os.path.exists(vaa_trades_file):
            try:
                with open(vaa_trades_file) as f:
                    trades = json.load(f)
            except Exception:
                pass

        dry_trades = [t for t in trades if t.get("dry_run")]
        n_signals  = len(dry_trades)

        # Anomalie-Checks
        anomalies = []

        # Check 1: Kein Trade mit risk > 25% (implausibles SL)
        for t in dry_trades:
            entry = t.get("entry_price", 0)
            sl    = t.get("sl", 0)
            if entry > 0 and sl > 0:
                risk_pct = abs(sl - entry) / entry
                if risk_pct > 0.25:
                    anomalies.append(f"{t['asset']}: SL-Distanz {risk_pct*100:.1f}% > 25%")

        # Check 2: Pending-Signale vorhanden (Bot scannt aktiv)
        pending = []
        if os.path.exists(vaa_pending_file):
            try:
                with open(vaa_pending_file) as f:
                    pending = json.load(f)
            except Exception:
                pass

        # Gate-Bewertung
        gate_passed = n_signals >= 5 and len(anomalies) == 0

        return {
            "n_signals":    n_signals,
            "n_pending":    len(pending),
            "gate_target":  5,
            "anomalies":    anomalies,
            "gate_passed":  gate_passed,
            "ready_for_live": gate_passed,
        }


# ─── Standalone-Report ────────────────────────────────────────────────────────

if __name__ == "__main__":
    guard = FactoryGuard()

    print("\n═══ APEX Factory Guard — Status ═══\n")

    # Daily DD
    dd = guard.get_dd_status()
    icons = {"OK": "✅", "HALF": "⚠️", "KILL": "🔴"}
    print(f"Daily DD  [{icons[dd['level']]}]")
    print(f"  Gesamt: {dd['total_r']:+.2f}R  "
          f"(Half bei {dd['half_at']}R | Kill bei {dd['kill_at']}R)")
    if dd["bots"]:
        for bot, r in dd["bots"].items():
            print(f"  {bot}: {r:+.2f}R")
    else:
        print("  Noch keine Trades heute")

    # API Rate
    print()
    api = guard.get_api_status()
    icons_a = {"OK": "✅", "WARN": "⚠️", "CRITICAL": "🔴"}
    print(f"API Rate  [{icons_a[api['level']]}]")
    print(f"  {api['calls_per_min']}/{api['limit_per_min']} Calls/min "
          f"({api['ratio_pct']}%)  |  Heute gesamt: {api['total_today']}")

    # VAA Gate
    print()
    vaa = guard.check_vaa_live_gate()
    gate_icon = "✅" if vaa["gate_passed"] else "🟡"
    print(f"VAA Live-Gate  [{gate_icon}]")
    print(f"  Signale: {vaa['n_signals']}/{vaa['gate_target']}  "
          f"Pending: {vaa['n_pending']}  "
          f"Bereit: {'JA' if vaa['ready_for_live'] else 'NEIN'}")
    if vaa["anomalies"]:
        for a in vaa["anomalies"]:
            print(f"  ⚠️  {a}")

    print()
