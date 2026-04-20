#!/usr/bin/env python3
"""
APEX Datenqualitäts-Check — läuft in /ASS vor filter_attribution.py

Tier 1 CRITICAL : blockiert filter_attribution (fehlendes Pflichtfeld nach Feature-Aktivierung)
Tier 2 WARNING  : zeigen, nicht blockieren (Plausibilitäts-Ausreißer)
Tier 3 INFO     : Coverage-Raten pro Hypothese (immer anzeigen)

Feature-Aktivierungsdaten steuern ab wann ein Feld PFLICHT ist.
Vor dem Aktivierungsdatum zählt fehlen als erwartet, nicht als Fehler.
"""
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
TRADES_FILE      = os.path.join(DATA_DIR, "trades.json")
SHADOW_LOG_FILE  = os.path.join(DATA_DIR, "hypothesis_shadow_log.jsonl")

# Datum ab dem ein Feld in JEDEM Trade vorhanden sein muss (ISO-String, inklusiv)
FEATURE_ACTIVE_FROM = {
    "volume_ratio":                        "2026-04-07",
    "body_ratio":                          "2026-04-12",
    "trend_context.ema_aligned":           "2026-04-13",
    "trend_context.h4_aligned":            "2026-04-13",
    "trend_context.is_squeezing":          "2026-04-18",
    "market_structure.or_mid_shift":       "2026-04-18",
    "regime_snapshot":                     "2026-04-19",
}


@dataclass
class DQReport:
    critical: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info:     list = field(default_factory=list)
    n_trades: int  = 0

    @property
    def score(self) -> str:
        if self.critical:
            return "CRITICAL"
        if self.warnings:
            return "WARNINGS"
        return "OK"


def _get(trade: dict, dotpath: str):
    """Navigiert a.b.c Pfade in einem dict. Gibt None zurück wenn nicht gefunden."""
    parts = dotpath.split(".")
    obj = trade
    for p in parts:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj


def _ts(trade: dict) -> str:
    return trade.get("timestamp", "")[:10]


def _active(trade: dict, feature: str) -> bool:
    """True wenn das Feature zum Zeitpunkt des Trades bereits aktiv war."""
    ts = _ts(trade)
    cutoff = FEATURE_ACTIVE_FROM.get(feature, "9999-99-99")
    return ts >= cutoff


def check_trades(trades: list) -> DQReport:
    report = DQReport()
    # Trades die einen Exit haben sollten (exit_timestamp gesetzt) aber kein exit_pnl_r
    exited = [t for t in trades if t.get("exit_timestamp")]
    done   = [t for t in exited if isinstance(t.get("exit_pnl_r"), (int, float))]
    report.n_trades = len(done)

    missing_exit_pnl = [t for t in exited if not isinstance(t.get("exit_pnl_r"), (int, float))]
    if missing_exit_pnl:
        report.critical.append(
            f"exit_pnl_r fehlt/ungültig in {len(missing_exit_pnl)} Trades mit exit_timestamp: "
            f"{[_ts(t) + ' ' + t.get('asset','?') for t in missing_exit_pnl]}"
        )

    if not done:
        report.info.append("Noch keine abgeschlossenen Trades mit exit_pnl_r.")
        return report

    missing_session = [t for t in done if not t.get("session")]
    if missing_session:
        report.critical.append(
            f"session fehlt in {len(missing_session)} Trades"
        )

    for feature in ["body_ratio", "trend_context.ema_aligned", "trend_context.h4_aligned",
                    "volume_ratio", "trend_context.is_squeezing",
                    "market_structure.or_mid_shift", "regime_snapshot"]:
        active_trades = [t for t in done if _active(t, feature)]
        if not active_trades:
            continue
        missing = [t for t in active_trades if _get(t, feature) is None]
        if missing:
            pct = len(missing) / len(active_trades) * 100
            # Nur CRITICAL wenn >10% der aktivierten Trades betroffen
            msg = (
                f"{feature} fehlt in {len(missing)}/{len(active_trades)} Trades "
                f"seit Aktivierung ({pct:.0f}%)"
            )
            if pct > 10:
                report.critical.append(msg)
            else:
                report.warnings.append(msg + " — einzelne Ausreißer, kein Systemfehler")

    # ── TIER 2: Plausibilitäts-Checks ───────────────────────────────────────
    for t in done:
        r = t.get("exit_pnl_r")
        if r is not None and (r < -5.0 or r > 10.0):
            report.warnings.append(
                f"exit_pnl_r={r}R außerhalb [-5R,+10R]: {_ts(t)} {t.get('asset')} — Datenfehler?"
            )

        br = t.get("body_ratio")
        if br is not None and not (0.0 <= br <= 1.0):
            report.warnings.append(
                f"body_ratio={br} außerhalb [0,1]: {_ts(t)} {t.get('asset')} — Berechnungsfehler?"
            )

        vr = t.get("volume_ratio")
        if vr is not None and vr > 20.0:
            report.warnings.append(
                f"volume_ratio={vr}x > 20x: {_ts(t)} {t.get('asset')} — Outlier?"
            )

        slip = t.get("slippage_usd")
        ep   = t.get("entry_price", 0)
        if slip is not None and ep and ep > 0 and abs(slip) / ep > 0.05:
            report.warnings.append(
                f"slippage={slip} > 5% von entry={ep}: {_ts(t)} {t.get('asset')} — API-Fehler?"
            )

        ets = t.get("exit_timestamp", "")
        ts  = t.get("timestamp", "")
        if ets and ts and ets < ts:
            report.warnings.append(
                f"exit_timestamp < entry_timestamp: {_ts(t)} {t.get('asset')} — Zeitfehler!"
            )

    # ── TIER 3: Coverage-Raten ───────────────────────────────────────────────
    def coverage(feature: str) -> str:
        active = [t for t in done if _active(t, feature)]
        if not active:
            return "n/a"
        have = [t for t in active if _get(t, feature) is not None]
        return f"{len(have)}/{len(active)} ({len(have)/len(active)*100:.0f}%)"

    report.info.append(
        f"Coverage (ab Feature-Aktivierung): "
        f"Body={coverage('body_ratio')} | "
        f"EMA={coverage('trend_context.ema_aligned')} | "
        f"Vol={coverage('volume_ratio')} | "
        f"Squeeze={coverage('trend_context.is_squeezing')} | "
        f"OR-Mid={coverage('market_structure.or_mid_shift')} | "
        f"Regime={coverage('regime_snapshot')}"
    )

    return report


def check_shadow_log() -> list:
    """Prüft ob hypothesis_shadow_log.jsonl existiert und aktuelle Einträge enthält."""
    issues = []
    if not os.path.exists(SHADOW_LOG_FILE):
        issues.append("hypothesis_shadow_log.jsonl fehlt — H-012/H-013 Shadow-Daten gehen verloren!")
        return issues

    entries = []
    try:
        with open(SHADOW_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception as e:
        issues.append(f"hypothesis_shadow_log.jsonl nicht lesbar: {e}")
        return issues

    h011 = [e for e in entries if e.get("hypothesis") == "H-011"]
    h012h013 = [e for e in entries if e.get("hypothesis") != "H-011"]

    today = datetime.now().strftime("%Y-%m-%d")
    recent = [e for e in h012h013 if e.get("timestamp", "")[:10] >= today]

    issues.append(
        f"Shadow-Log: {len(entries)} Einträge gesamt | "
        f"H-011={len(h011)} | H-012/H-013={len(h012h013)} | heute={len(recent)}"
    )
    return issues


def main():
    if not os.path.exists(TRADES_FILE):
        print("⚠️  trades.json nicht gefunden")
        sys.exit(0)

    try:
        with open(TRADES_FILE) as f:
            trades = json.load(f)
    except Exception as e:
        print(f"🔴 trades.json nicht lesbar: {e}")
        sys.exit(1)

    report = check_trades(trades)
    shadow_info = check_shadow_log()

    # Ausgabe
    icon = {"OK": "✅", "WARNINGS": "⚠️ ", "CRITICAL": "🔴"}[report.score]
    print(f"{icon} Datenqualität: {report.n_trades} Trades | "
          f"{len(report.critical)} kritisch | {len(report.warnings)} Warnings")

    for msg in report.critical:
        print(f"   🔴 CRITICAL: {msg}")
    for msg in report.warnings:
        print(f"   ⚠️  WARNING: {msg}")
    for msg in report.info:
        print(f"   📊 {msg}")
    for msg in shadow_info:
        print(f"   🗂  {msg}")

    # Exit-Code: 2 = critical (für /ASS-Logik), 0 = ok/warnings
    if report.critical:
        sys.exit(2)


if __name__ == "__main__":
    main()
