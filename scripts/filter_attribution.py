#!/usr/bin/env python3
"""
Filter Attribution Analysis — Phase A.1

Beantwortet: Welcher Filter trägt tatsächlich Edge?

Logik: Für jeden bekannten Filter (H-006 EMA, H-014 Volume, etc.):
  - Partitioniere geschlossene Trades nach Filter-Flag TRUE/FALSE
  - Berechne Avg-R, Win-Rate, Total-R pro Gruppe
  - Bootstrap-CI95 für Avg-R-Differenz
  - Output: Ampel-Verdikt (KEEP / REVIEW / KILL)

Verwendung: python3 filter_attribution.py [--json]
Integration: Wird von /asa PHASE 3.5 aufgerufen.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from statistics import mean
from typing import Callable

TRADES_FILE = Path("/root/apex-trading-bot/data/trades.json")
BOOTSTRAP_N = 2000
CI_LOW, CI_HIGH = 2.5, 97.5
MIN_N_FOR_VERDICT = 5


def load_closed_trades() -> list[dict]:
    trades = json.loads(TRADES_FILE.read_text())
    return [t for t in trades if t.get("exit_pnl_r") is not None]


def extract_flag(trade: dict, path: list[str]) -> object:
    """Traversiere nested dict-Pfad. Returnt None wenn irgendwo fehlt."""
    node: object = trade
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node


def bootstrap_ci_diff(group_true: list[float], group_false: list[float]) -> tuple[float, float]:
    """Bootstrap 95%-CI für Mittelwerts-Differenz (true - false)."""
    if not group_true or not group_false:
        return (float("nan"), float("nan"))
    diffs = []
    for _ in range(BOOTSTRAP_N):
        t = [random.choice(group_true) for _ in group_true]
        f = [random.choice(group_false) for _ in group_false]
        diffs.append(mean(t) - mean(f))
    diffs.sort()
    lo = diffs[int(BOOTSTRAP_N * CI_LOW / 100)]
    hi = diffs[int(BOOTSTRAP_N * CI_HIGH / 100)]
    return (lo, hi)


def verdict(n_true: int, n_false: int, diff: float, ci_lo: float, ci_hi: float) -> str:
    if n_true < MIN_N_FOR_VERDICT or n_false < MIN_N_FOR_VERDICT:
        return "INSUFFICIENT"
    if ci_lo > 0:
        return "KEEP"
    if ci_hi < 0:
        return "KILL"
    if diff > 0:
        return "REVIEW+"
    return "REVIEW-"


# Filter-Definitionen: (ID, Label, Pfad-zum-Flag, optional Wert-Mapper)
FILTERS: list[tuple[str, str, list[str], Callable[[object], bool] | None]] = [
    ("H-006a", "EMA-15m aligned", ["trend_context", "ema_aligned"], None),
    ("H-006b", "H4 aligned", ["trend_context", "h4_aligned"], None),
    ("H-014", "Volume >= 1.0x", ["volume_ratio"], lambda v: isinstance(v, (int, float)) and v >= 1.0),
    ("H-012", "OR-Mid bias_aligned", ["market_structure", "or_mid_shift", "bias_aligned"], None),
    ("H-013", "Squeeze active", ["trend_context", "is_squeezing"], None),
    ("H-009", "Body >= 30%", ["body_ratio"], lambda v: isinstance(v, (int, float)) and v >= 0.30),
]


def analyze(trades: list[dict]) -> list[dict]:
    results = []
    for h_id, label, path, mapper in FILTERS:
        group_true, group_false = [], []
        for t in trades:
            raw = extract_flag(t, path)
            flag = mapper(raw) if mapper else raw
            if flag is None:
                continue
            r = t["exit_pnl_r"]
            (group_true if flag else group_false).append(r)
        n_t, n_f = len(group_true), len(group_false)
        if n_t + n_f == 0:
            results.append({"id": h_id, "label": label, "n_true": 0, "n_false": 0,
                            "verdict": "NO_DATA"})
            continue
        avg_t = mean(group_true) if group_true else float("nan")
        avg_f = mean(group_false) if group_false else float("nan")
        diff = (avg_t - avg_f) if (group_true and group_false) else float("nan")
        ci_lo, ci_hi = bootstrap_ci_diff(group_true, group_false)
        wr_t = sum(1 for r in group_true if r > 0) / n_t if n_t else float("nan")
        wr_f = sum(1 for r in group_false if r > 0) / n_f if n_f else float("nan")
        results.append({
            "id": h_id, "label": label,
            "n_true": n_t, "n_false": n_f,
            "avg_r_true": avg_t, "avg_r_false": avg_f,
            "diff": diff, "ci95_lo": ci_lo, "ci95_hi": ci_hi,
            "wr_true": wr_t, "wr_false": wr_f,
            "total_r_true": sum(group_true), "total_r_false": sum(group_false),
            "verdict": verdict(n_t, n_f, diff, ci_lo, ci_hi),
        })
    return results


def render_table(results: list[dict]) -> str:
    lines = [
        "Filter Attribution — Counterfactual R-Impact",
        "=" * 88,
        f"{'ID':<8} {'Filter':<24} {'n(T/F)':<10} {'AvgR T/F':<18} {'Diff':>7} {'CI95':<18} {'Verdict':<12}",
        "-" * 88,
    ]
    for r in results:
        if r["verdict"] == "NO_DATA":
            lines.append(f"{r['id']:<8} {r['label']:<24} {'—':<10} {'— / —':<18} {'—':>7} {'—':<18} NO_DATA")
            continue
        n_pair = f"{r['n_true']}/{r['n_false']}"
        avg_pair = f"{r['avg_r_true']:+.2f} / {r['avg_r_false']:+.2f}"
        diff = f"{r['diff']:+.2f}" if r['diff'] == r['diff'] else "—"
        ci = f"[{r['ci95_lo']:+.2f}, {r['ci95_hi']:+.2f}]" if r['ci95_lo'] == r['ci95_lo'] else "—"
        lines.append(f"{r['id']:<8} {r['label']:<24} {n_pair:<10} {avg_pair:<18} {diff:>7} {ci:<18} {r['verdict']:<12}")
    lines.append("")
    lines.append("Verdict-Legende:")
    lines.append("  KEEP         CI95(true-false) > 0 → Filter trägt statistisch Edge")
    lines.append("  KILL         CI95(true-false) < 0 → Filter schadet statistisch")
    lines.append("  REVIEW+      Positive Tendenz, CI95 überlappt 0 (mehr Daten)")
    lines.append("  REVIEW-      Negative Tendenz, CI95 überlappt 0 (mehr Daten)")
    lines.append("  INSUFFICIENT n<5 in einer Gruppe")
    lines.append("  NO_DATA      Feld nicht in Trades vorhanden")
    return "\n".join(lines)


def main() -> int:
    trades = load_closed_trades()
    if not trades:
        print("Keine geschlossenen Trades gefunden.", file=sys.stderr)
        return 1
    random.seed(42)
    results = analyze(trades)
    if "--json" in sys.argv:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(render_table(results))
        print(f"\nAnalysiert: {len(trades)} geschlossene Trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
