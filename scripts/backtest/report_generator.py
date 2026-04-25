#!/usr/bin/env python3
"""
Phase 1.6 — Standard-Report-Generator.

Erzeugt Markdown-Reports aus Backtest-Output:
  - Grund-Kennzahlen (n, WR, Avg R, Total R, PF, Sharpe, Max DD)
  - ASCII-Equity-Curve
  - IS/OOS-Vergleich (optional)
  - Monte Carlo Perzentile (optional)
  - Bonferroni-Status (optional)

Speichert nach data/backtest_reports/{run_id}.md

Verwendung als Modul:
  from scripts.backtest.report_generator import generate_report
  generate_report(run_id, trades, wfa=..., mc=..., tester=...)
"""
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

REPORT_DIR = os.path.join(PROJECT_DIR, "data", "backtest_reports")


def kpis(r_list: list[float]) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n": 0}
    wins  = [r for r in r_list if r > 0]
    loses = [r for r in r_list if r <= 0]
    total = sum(r_list)
    gw, gl = sum(wins), abs(sum(loses))
    mean = total / n
    var  = sum((r - mean) ** 2 for r in r_list) / (n - 1) if n > 1 else 0
    sd   = math.sqrt(var)
    # Max DD
    peak, cum, dd_max = 0.0, 0.0, 0.0
    for r in r_list:
        cum += r
        if cum > peak:
            peak = cum
        dd_max = max(dd_max, peak - cum)
    return {
        "n":        n,
        "wr":       len(wins) / n,
        "avg_r":    mean,
        "total_r":  total,
        "pf":       gw / gl if gl > 0 else float("inf"),
        "sharpe":   mean / sd if sd > 0 else 0,
        "max_dd":   dd_max,
    }


def ascii_equity_curve(r_list: list[float], width: int = 60, height: int = 12) -> str:
    """Kompakte ASCII-Equity-Curve."""
    if not r_list:
        return "(no data)"
    cum = []
    acc = 0.0
    for r in r_list:
        acc += r
        cum.append(acc)
    # Downsample auf width
    step = max(1, len(cum) // width)
    sampled = cum[::step][:width]
    hi, lo = max(sampled), min(sampled)
    rng = max(hi - lo, 1e-9)
    # Grid
    grid = [[" "] * len(sampled) for _ in range(height)]
    for x, v in enumerate(sampled):
        y_norm = (v - lo) / rng
        y = int((1 - y_norm) * (height - 1))
        y = max(0, min(height - 1, y))
        grid[y][x] = "█"
    # Zero-Linie
    if lo < 0 < hi:
        zero_y = int((1 - (0 - lo) / rng) * (height - 1))
        for x in range(len(sampled)):
            if grid[zero_y][x] == " ":
                grid[zero_y][x] = "-"
    lines = ["".join(row) for row in grid]
    # Y-Achse Labels
    out = [f"  {hi:>+8.2f}R  " + lines[0]]
    for line in lines[1:-1]:
        out.append(" " * 12 + line)
    out.append(f"  {lo:>+8.2f}R  " + lines[-1])
    return "\n".join(out)


def format_kpi_table(k: dict) -> str:
    if k.get("n", 0) == 0:
        return "| (keine Trades) |\n"
    pf_str = f"{k['pf']:.2f}" if k["pf"] != float("inf") else "∞"
    return (
        "| Kennzahl | Wert |\n"
        "|---|---|\n"
        f"| n Trades | {k['n']} |\n"
        f"| Win-Rate | {k['wr']*100:.1f}% |\n"
        f"| Avg R | {k['avg_r']:+.4f}R |\n"
        f"| Total R | {k['total_r']:+.2f}R |\n"
        f"| Profit Factor | {pf_str} |\n"
        f"| Sharpe (trade-level) | {k['sharpe']:.3f} |\n"
        f"| Max Drawdown | {k['max_dd']:.2f}R |\n"
    )


def generate_report(run_id: str, r_list: list[float],
                    title: str = "Backtest-Report",
                    strategy_config: dict = None,
                    wfa_result: dict = None,
                    mc_result: dict = None,
                    tester_result: dict = None,
                    asset_breakdown: dict = None,
                    regime_breakdown: dict = None,
                    additional_sections: dict = None) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{run_id}.md")

    lines = []
    lines.append(f"# {title}")
    lines.append(f"")
    lines.append(f"**Run-ID:** `{run_id}`  ")
    lines.append(f"**Generiert:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"")

    if strategy_config:
        lines.append(f"## Strategie-Konfiguration")
        lines.append(f"")
        for k, v in strategy_config.items():
            lines.append(f"- **{k}**: `{v}`")
        lines.append(f"")

    # Haupt-KPIs
    k = kpis(r_list)
    lines.append(f"## Kennzahlen")
    lines.append(f"")
    lines.append(format_kpi_table(k))

    # Equity-Curve
    if r_list:
        lines.append(f"## Equity-Curve (kumulativ R)")
        lines.append(f"")
        lines.append(f"```")
        lines.append(ascii_equity_curve(r_list))
        lines.append(f"```")
        lines.append(f"")

    # Walk-Forward
    if wfa_result:
        lines.append(f"## Walk-Forward-Analyse")
        lines.append(f"")
        lines.append(f"| Fold | IS-Range | OOS-Range | n_IS | n_OOS | Param | IS Avg R | OOS Avg R |")
        lines.append(f"|---|---|---|---|---|---|---|---|")
        for f in wfa_result.get("folds", []):
            p = "—" if f["best_param"] is None else f"{f['best_param']:.2f}"
            lines.append(f"| {f['fold']} | {f['is_range']} | {f['oos_range']} | "
                         f"{f['n_is']} | {f['n_oos']} | {p} | "
                         f"{f['is_avg_r']:+.4f}R | {f['oos_avg_r']:+.4f}R |")
        lines.append(f"")
        lines.append(f"- **Mean IS Avg R:** {wfa_result['mean_is_r']:+.4f}R")
        lines.append(f"- **Mean OOS Avg R:** {wfa_result['mean_oos_r']:+.4f}R")
        wfe = wfa_result.get("wfe")
        wfe_str = f"{wfe:.3f}" if wfe is not None else "n/a"
        lines.append(f"- **WFE (OOS/IS):** {wfe_str}")
        lines.append(f"- **Positive OOS-Folds:** {wfa_result['positive_folds']}/{wfa_result['total_folds']}")
        lines.append(f"")

    # Monte Carlo
    if mc_result:
        lines.append(f"## Monte Carlo ({mc_result['iterations']:,} Iterations, mode={mc_result.get('mode','bootstrap')})")
        lines.append(f"")
        lines.append(f"| Kennzahl | Realized | P5 | P50 | P95 |")
        lines.append(f"|---|---|---|---|---|")
        rz = mc_result["realized"]
        lines.append(f"| Final R | {rz['final_r']:+.3f} | {mc_result['final_r']['p5']:+.3f} | "
                     f"{mc_result['final_r']['p50']:+.3f} | {mc_result['final_r']['p95']:+.3f} |")
        lines.append(f"| Max DD R | {rz['max_dd_r']:+.3f} | {mc_result['max_dd_r']['p5']:+.3f} | "
                     f"{mc_result['max_dd_r']['p50']:+.3f} | {mc_result['max_dd_r']['p95']:+.3f} |")
        lines.append(f"| Sharpe | {rz['sharpe']:+.3f} | {mc_result['sharpe']['p5']:+.3f} | "
                     f"{mc_result['sharpe']['p50']:+.3f} | {mc_result['sharpe']['p95']:+.3f} |")
        lines.append(f"")

    # Hypothesis-Tests (Bonferroni)
    if tester_result and tester_result.get("results"):
        lines.append(f"## Hypothesis-Tests (Bonferroni)")
        lines.append(f"")
        lines.append(f"α = {tester_result['alpha']:.4f}, α_adj = {tester_result['alpha_adj']:.5f}, "
                     f"n_tests = {tester_result['n_tests']}")
        lines.append(f"")
        lines.append(f"| ID | n | Mean R | t | p | Verdict |")
        lines.append(f"|---|---|---|---|---|---|")
        for r in tester_result["results"]:
            lines.append(f"| {r['id']} | {r['n']} | {r['mean_r']:+.3f}R | "
                         f"{r['t']:+.2f} | {r['p']:.5f} | {r['verdict']} |")
        lines.append(f"")

    # Asset-Breakdown
    if asset_breakdown:
        lines.append(f"## Asset-Breakdown")
        lines.append(f"")
        lines.append(f"| Asset | n | WR | Avg R | Total R |")
        lines.append(f"|---|---|---|---|---|")
        for asset, a in asset_breakdown.items():
            lines.append(f"| {asset} | {a['n']} | {a['wr']*100:.1f}% | "
                         f"{a['avg_r']:+.3f}R | {a['total_r']:+.2f}R |")
        lines.append(f"")

    # Regime-Breakdown
    if regime_breakdown:
        lines.append(f"## Regime-Breakdown")
        lines.append(f"")
        lines.append(f"| Regime | n | WR | Avg R | Total R |")
        lines.append(f"|---|---|---|---|---|")
        for regime, s in regime_breakdown.items():
            lines.append(f"| {regime} | {s['n']} | {s['wr']*100:.1f}% | "
                         f"{s['avg_r']:+.3f}R | {s['total_r']:+.2f}R |")
        lines.append(f"")

    # Zusätzliche Custom-Sections
    if additional_sections:
        for title, content in additional_sections.items():
            lines.append(f"## {title}")
            lines.append(f"")
            lines.append(content)
            lines.append(f"")

    content = "\n".join(lines) + "\n"
    with open(path, "w") as f:
        f.write(content)
    return path


if __name__ == "__main__":
    # Smoke-Test mit Baseline
    from scripts.backtest.regime_breakdown import load_trades, resolve_r
    from scripts.backtest.walk_forward import run_wfa
    from scripts.backtest.monte_carlo import run_monte_carlo

    trades = load_trades()
    r_list = [resolve_r(t, "baseline_2r", 0.5) for t in trades]

    print(f"🧪 Smoke-Test Report-Generator")
    print(f"   Trades: {len(r_list)}")

    wfa = run_wfa(trades, "baseline_2r")
    mc  = run_monte_carlo(r_list, iterations=2000)

    path = generate_report(
        run_id="phase1_baseline_smoke",
        r_list=r_list,
        title="Phase 1 Smoke-Test — Baseline PDH/PDL",
        strategy_config={
            "exit_mode":    "baseline_2r",
            "assets":       "BTC, ETH, SOL, AVAX, XRP, DOGE, ADA, LINK, SUI, AAVE",
            "period":       "2025-04-21 → 2026-04-19",
            "sl_buffer":    "0.1% vom PDH/PDL",
            "tp":           "2R fix",
        },
        wfa_result=wfa,
        mc_result=mc,
    )
    print(f"   Report: {path}")
