#!/usr/bin/env python3
"""
Hypothesis Killer — Phase A.2

Beantwortet: Welche Hypothese sollte ich JETZT killen? Welche promoten?

Logik: Bayesian Beta-Binomial auf Win-Rate + Bootstrap auf Avg-R.
  - Prior: Beta(1, 1) (uniform — keine Vorannahme)
  - Posterior: Beta(1 + wins, 1 + losses)
  - P(true_WR > 0.5) aus Posterior
  - Kill wenn P(edge>0) < 10% und n >= 5
  - Promote wenn P(edge>0) > 90% und n >= 10

Verwendung: python3 hypothesis_killer.py [--json]
Integration: Wird von /ASS Schritt 1 aufgerufen.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

TRADES_FILE = Path("/root/apex-trading-bot/data/trades.json")
HYPOTHESIS_LOG = Path("/root/.claude/projects/-root-apex-trading-bot/memory/hypothesis_log.md")
BOOTSTRAP_N = 5000
KILL_THRESHOLD = 0.10
PROMOTE_THRESHOLD = 0.90
MIN_N_KILL = 5
MIN_N_PROMOTE = 10

# Hypothesen mit Commit-Datum, Filter-Flag-Pfad und Filter-Wert-Logik
# Format: (H-ID, Label, start_date, filter-check-function)
# filter-check: nimmt trade dict, gibt True wenn Trade DURCH Filter gekommen wäre
#               (bzw. True wenn Hypothese "aktiv" war)


def filter_ema_aligned(t: dict) -> bool | None:
    """H-006: Trade durchgelassen wenn ema_aligned=True"""
    v = t.get("trend_context", {}).get("ema_aligned")
    return bool(v) if v is not None else None


def filter_volume_ge1(t: dict) -> bool | None:
    """H-014: Trade durchgelassen wenn volume_ratio >= 1.0"""
    v = t.get("volume_ratio")
    if v is None or not isinstance(v, (int, float)):
        return None
    return v >= 1.0


def filter_static_tp2(t: dict) -> bool:
    """H-002: Alle Trades ab Commit-Datum waren unter statischem TP2"""
    return True


def filter_cron5min(t: dict) -> bool:
    """H-008: Alle Trades ab Commit-Datum unter 5-Min-Cron"""
    return True


HYPOTHESES = [
    # (ID, Label, Commit/Live-Datum ISO, filter-check, Mindest-Gate-n)
    ("H-002", "Static TP2 @ 3R",      "2026-04-09", filter_static_tp2, 10),
    ("H-006", "EMA-15m aligned",      "2026-04-16", filter_ema_aligned, 10),
    ("H-008", "5-Min Cron",           "2026-04-11", filter_cron5min,    5),
    ("H-014", "Volume >= 1.0x",       "2026-04-18", filter_volume_ge1, 10),
]


def load_closed_trades() -> list[dict]:
    trades = json.loads(TRADES_FILE.read_text())
    return [t for t in trades if t.get("exit_pnl_r") is not None]


def beta_quantile(alpha: float, beta_param: float, q: float) -> float:
    """
    Sample-basierte Quantile aus Beta(alpha, beta).
    Verwendet random.betavariate — keine scipy-Abhängigkeit.
    """
    samples = sorted(random.betavariate(alpha, beta_param) for _ in range(BOOTSTRAP_N))
    idx = int(BOOTSTRAP_N * q)
    return samples[min(idx, BOOTSTRAP_N - 1)]


def prob_edge_positive(wins: int, losses: int) -> float:
    """P(True-WR > 0.5) via Posterior Beta(1+W, 1+L)."""
    if wins + losses == 0:
        return 0.5
    a = 1 + wins
    b = 1 + losses
    samples = [random.betavariate(a, b) for _ in range(BOOTSTRAP_N)]
    return sum(1 for s in samples if s > 0.5) / BOOTSTRAP_N


def bootstrap_mean_ci(values: list[float]) -> tuple[float, float]:
    """95%-CI für Mittelwert via Bootstrap."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    means = []
    for _ in range(BOOTSTRAP_N):
        means.append(mean(random.choice(values) for _ in values))
    means.sort()
    return (means[int(BOOTSTRAP_N * 0.025)], means[int(BOOTSTRAP_N * 0.975)])


def evaluate_hypothesis(h_id: str, label: str, start_date: str,
                        filter_fn, gate_n: int, trades: list[dict]) -> dict:
    start_ts = datetime.fromisoformat(start_date)
    relevant = []
    for t in trades:
        ts_str = t.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", ""))
        except (ValueError, AttributeError):
            continue
        if ts < start_ts:
            continue
        passes = filter_fn(t)
        if passes is None:
            continue
        if passes:
            relevant.append(t)
    n = len(relevant)
    if n == 0:
        return {"id": h_id, "label": label, "n": 0, "action": "WAIT",
                "reason": "keine Trades seit Live-Datum mit Filter-Flag"}
    rs = [t["exit_pnl_r"] for t in relevant]
    wins = sum(1 for r in rs if r > 0)
    losses = n - wins
    avg_r = mean(rs)
    total_r = sum(rs)
    p_edge = prob_edge_positive(wins, losses)
    ci_lo, ci_hi = bootstrap_mean_ci(rs)
    # Action
    if n < MIN_N_KILL:
        action, reason = "WAIT", f"n={n} < {MIN_N_KILL}"
    elif p_edge < KILL_THRESHOLD:
        action, reason = "KILL", f"P(edge>0)={p_edge:.0%} < {KILL_THRESHOLD:.0%}"
    elif p_edge > PROMOTE_THRESHOLD and n >= MIN_N_PROMOTE:
        action, reason = "PROMOTE", f"P(edge>0)={p_edge:.0%} > {PROMOTE_THRESHOLD:.0%}"
    elif n >= gate_n:
        action, reason = "GATE_REACHED", f"n={n} >= Gate {gate_n}, Andre entscheidet"
    else:
        action, reason = "CONTINUE", f"Gate in {gate_n - n} Trades"
    return {
        "id": h_id, "label": label,
        "n": n, "wins": wins, "losses": losses,
        "win_rate": wins / n,
        "avg_r": avg_r, "total_r": total_r,
        "ci95_lo": ci_lo, "ci95_hi": ci_hi,
        "p_edge_positive": p_edge,
        "action": action, "reason": reason,
        "gate_n": gate_n,
    }


def render_table(results: list[dict]) -> str:
    lines = [
        "Hypothesis Killer — Bayesian Edge-Evaluation",
        "=" * 92,
        f"{'ID':<7} {'Label':<22} {'n':>3} {'WR':>5} {'AvgR':>7} {'CI95':<18} {'P(edge)':>8} {'Action':<14}",
        "-" * 92,
    ]
    for r in results:
        if r["n"] == 0:
            lines.append(f"{r['id']:<7} {r['label']:<22} {0:>3} {'—':>5} {'—':>7} {'—':<18} {'—':>8} {r['action']:<14}")
            continue
        ci = f"[{r['ci95_lo']:+.2f},{r['ci95_hi']:+.2f}]" if r['ci95_lo'] == r['ci95_lo'] else "—"
        lines.append(
            f"{r['id']:<7} {r['label']:<22} {r['n']:>3} "
            f"{r['win_rate']:>4.0%} {r['avg_r']:>+6.2f}R {ci:<18} "
            f"{r['p_edge_positive']:>7.0%} {r['action']:<14}"
        )
    lines.append("")
    lines.append("Action-Legende:")
    lines.append("  KILL         P(edge>0) < 10% → sofort zurückbauen/demoten")
    lines.append("  PROMOTE      P(edge>0) > 90% und n≥10 → zu `verified` + Status-Update")
    lines.append("  GATE_REACHED n >= Gate → Andre muss entscheiden (Deep Review fällig)")
    lines.append("  CONTINUE     Daten sammeln bis Gate")
    lines.append("  WAIT         n<5 — zu wenig Daten")
    return "\n".join(lines)


def render_action_summary(results: list[dict]) -> str:
    kills = [r for r in results if r["action"] == "KILL"]
    promotes = [r for r in results if r["action"] == "PROMOTE"]
    gates = [r for r in results if r["action"] == "GATE_REACHED"]
    if not (kills or promotes or gates):
        return "Keine fälligen Entscheidungen."
    lines = []
    for r in kills:
        lines.append(f"🔴 KILL {r['id']}: {r['reason']} (AvgR {r['avg_r']:+.2f})")
    for r in promotes:
        lines.append(f"🟢 PROMOTE {r['id']}: {r['reason']} (AvgR {r['avg_r']:+.2f})")
    for r in gates:
        lines.append(f"🟡 GATE {r['id']}: {r['reason']} (AvgR {r['avg_r']:+.2f})")
    return "\n".join(lines)


def main() -> int:
    random.seed(42)
    trades = load_closed_trades()
    results = [evaluate_hypothesis(h_id, label, start, fn, gate, trades)
               for h_id, label, start, fn, gate in HYPOTHESES]
    if "--json" in sys.argv:
        print(json.dumps(results, indent=2, default=str))
    elif "--summary" in sys.argv:
        print(render_action_summary(results))
    else:
        print(render_table(results))
        print()
        print(render_action_summary(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
