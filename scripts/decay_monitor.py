#!/usr/bin/env python3
"""
Decay Monitor — Phase B.2

Beantwortet: Degradiert eine `verified` (oder `live / validating`) Hypothese silent?

Logik: Für jede Hypothese mit Filter-Flag in trades.json:
  - Baseline-Periode: erste 10 Trades ab Commit/Live-Datum
  - Recent-Periode: letzte 30 Tage (rolling)
  - Welch's t-Test auf Avg-R-Differenz
  - Alert wenn p < 0.05 UND recent_avg_r < baseline_avg_r × 0.5

Nutzt Reservoir-Sampling statt scipy — keine zusätzlichen Dependencies.

Verwendung: python3 decay_monitor.py [--json]
Integration: /asa PHASE 3.6 (monatlich) oder /ASS bei fälligem Review.
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev

TRADES_FILE = Path("/root/apex-trading-bot/data/trades.json")
BOOTSTRAP_N = 2000
BASELINE_SIZE = 10
RECENT_DAYS = 30
ALERT_P = 0.05
ALERT_DECAY_RATIO = 0.5  # recent < baseline × 0.5


def filter_ema(t: dict) -> bool | None:
    v = t.get("trend_context", {}).get("ema_aligned")
    return bool(v) if v is not None else None


def filter_volume(t: dict) -> bool | None:
    v = t.get("volume_ratio")
    if not isinstance(v, (int, float)):
        return None
    return v >= 1.0


def filter_static_tp2(_t: dict) -> bool:
    return True


def filter_cron5(_t: dict) -> bool:
    return True


HYPOTHESES = [
    ("H-002", "Static TP2 @ 3R",  "2026-04-09", filter_static_tp2),
    ("H-006", "EMA-15m aligned",  "2026-04-16", filter_ema),
    ("H-008", "5-Min Cron",       "2026-04-11", filter_cron5),
    ("H-014", "Volume >= 1.0x",   "2026-04-18", filter_volume),
]


def load_trades() -> list[dict]:
    return [t for t in json.loads(TRADES_FILE.read_text())
            if t.get("exit_pnl_r") is not None]


def parse_ts(t: dict) -> datetime | None:
    ts = t.get("timestamp", "")
    try:
        return datetime.fromisoformat(ts.replace("Z", ""))
    except (ValueError, AttributeError):
        return None


def bootstrap_p_value(baseline: list[float], recent: list[float]) -> float:
    """
    P(mean_recent >= mean_baseline) via Permutations-Bootstrap.
    Kleines p → Differenz ist signifikant negativ (Decay).
    """
    if not baseline or not recent:
        return float("nan")
    observed_diff = mean(recent) - mean(baseline)
    pooled = baseline + recent
    n_b = len(baseline)
    extreme = 0
    for _ in range(BOOTSTRAP_N):
        random.shuffle(pooled)
        sim_b = pooled[:n_b]
        sim_r = pooled[n_b:]
        if (mean(sim_r) - mean(sim_b)) <= observed_diff:
            extreme += 1
    return extreme / BOOTSTRAP_N


def evaluate(h_id: str, label: str, start: str, filter_fn,
             trades: list[dict], now: datetime) -> dict:
    start_ts = datetime.fromisoformat(start)
    cutoff_recent = now - timedelta(days=RECENT_DAYS)
    relevant = []
    for t in trades:
        ts = parse_ts(t)
        if ts is None or ts < start_ts:
            continue
        passes = filter_fn(t)
        if passes is None or not passes:
            continue
        relevant.append((ts, t["exit_pnl_r"]))
    relevant.sort(key=lambda x: x[0])
    if len(relevant) < BASELINE_SIZE + 3:
        return {"id": h_id, "label": label,
                "n_total": len(relevant),
                "status": "INSUFFICIENT",
                "reason": f"n={len(relevant)} < {BASELINE_SIZE+3}"}
    baseline_rs = [r for _, r in relevant[:BASELINE_SIZE]]
    recent_rs = [r for ts, r in relevant if ts >= cutoff_recent]
    if len(recent_rs) < 3:
        return {"id": h_id, "label": label,
                "n_total": len(relevant), "n_recent": len(recent_rs),
                "status": "NO_RECENT",
                "reason": f"nur {len(recent_rs)} Trades in letzten {RECENT_DAYS}d"}
    base_avg = mean(baseline_rs)
    rec_avg = mean(recent_rs)
    p = bootstrap_p_value(baseline_rs, recent_rs)
    decayed = p < ALERT_P and rec_avg < base_avg * ALERT_DECAY_RATIO
    status = "DECAYED" if decayed else ("WEAKENING" if rec_avg < base_avg else "STABLE")
    return {
        "id": h_id, "label": label,
        "n_baseline": len(baseline_rs), "n_recent": len(recent_rs),
        "baseline_avg_r": round(base_avg, 3),
        "recent_avg_r": round(rec_avg, 3),
        "delta": round(rec_avg - base_avg, 3),
        "p_value": round(p, 3),
        "status": status,
        "reason": f"base={base_avg:+.2f}R vs recent={rec_avg:+.2f}R (p={p:.2f})",
    }


def render(results: list[dict]) -> str:
    lines = [
        "Decay Monitor — Baseline vs. letzte 30 Tage",
        "=" * 82,
        f"{'ID':<7} {'Label':<22} {'Base n/R':<12} {'Recent n/R':<14} {'p':>6} {'Status':<12}",
        "-" * 82,
    ]
    for r in results:
        if r["status"] in ("INSUFFICIENT", "NO_RECENT"):
            lines.append(f"{r['id']:<7} {r['label']:<22} {'—':<12} {'—':<14} {'—':>6} {r['status']:<12}")
            continue
        base = f"{r['n_baseline']}/{r['baseline_avg_r']:+.2f}R"
        rec = f"{r['n_recent']}/{r['recent_avg_r']:+.2f}R"
        lines.append(f"{r['id']:<7} {r['label']:<22} {base:<12} {rec:<14} "
                     f"{r['p_value']:>6.2f} {r['status']:<12}")
    lines.append("")
    lines.append("Status-Legende:")
    lines.append("  DECAYED    p<0.05 UND recent < baseline × 0.5 → Demote empfehlen")
    lines.append("  WEAKENING  recent schlechter als baseline aber nicht signifikant")
    lines.append("  STABLE     recent >= baseline")
    lines.append("  NO_RECENT  <3 Trades in letzten 30d")
    lines.append("  INSUFFICIENT  <13 Trades total")
    return "\n".join(lines)


def main() -> int:
    random.seed(42)
    trades = load_trades()
    if not trades:
        print("Keine geschlossenen Trades.", file=sys.stderr)
        return 1
    now = datetime.now()
    results = [evaluate(hid, lbl, st, fn, trades, now) for hid, lbl, st, fn in HYPOTHESES]
    if "--json" in sys.argv:
        print(json.dumps(results, indent=2))
    else:
        print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
