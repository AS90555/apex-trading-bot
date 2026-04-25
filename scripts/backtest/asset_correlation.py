#!/usr/bin/env python3
"""
Phase 0.5 — Asset-Korrelations-Check für PDH/PDL-Assets.

Berechnet Pearson-Korrelation der täglichen Returns pro Asset-Paar.
Markiert Paare mit r > 0.85 als redundant (für Phase 4 Asset-Selektion).

Verwendung:
  python3 scripts/backtest/asset_correlation.py
  python3 scripts/backtest/asset_correlation.py --from 2025-04-21 --to 2026-04-19
"""
import argparse
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, aggregate_daily

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "AVAX", "XRP", "DOGE", "ADA", "LINK", "SUI", "AAVE"]


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx  = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy  = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def daily_returns(asset: str, start: str, end: str) -> dict:
    candles = load_csv(asset, "15m")
    if not candles:
        return {}
    daily = aggregate_daily(candles)
    days = sorted(daily.keys())
    returns = {}
    prev_close = None
    for day in days:
        if day < start or day > end:
            continue
        close = daily[day]["close"]
        if prev_close is not None and prev_close > 0:
            returns[day] = close / prev_close - 1
        prev_close = close
    return returns


def build_matrix(assets: list[str], start: str, end: str) -> tuple[list[list[float]], int]:
    all_returns = {a: daily_returns(a, start, end) for a in assets}
    # gemeinsame Tage
    common_days = set(all_returns[assets[0]].keys())
    for a in assets[1:]:
        common_days &= set(all_returns[a].keys())
    common_sorted = sorted(common_days)
    series = {a: [all_returns[a][d] for d in common_sorted] for a in assets}

    n = len(assets)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 1.0
            else:
                matrix[i][j] = pearson(series[assets[i]], series[assets[j]])
    return matrix, len(common_sorted)


def print_matrix(assets: list[str], matrix: list[list[float]]):
    print(f"\n  === Pearson-Korrelations-Matrix (Daily Returns) ===")
    header = "  " + "     " + "".join(f"{a:>6}" for a in assets)
    print(header)
    for i, a in enumerate(assets):
        row = [f"{matrix[i][j]:>+6.2f}" for j in range(len(assets))]
        print(f"  {a:<5} {''.join(row)}")


def find_clusters(assets: list[str], matrix: list[list[float]], threshold: float) -> list[list[str]]:
    """Greedy-Clustering: Assets mit r ≥ threshold zu einem anderen Cluster-Mitglied."""
    n = len(assets)
    clusters = []
    assigned = set()
    for i in range(n):
        if i in assigned:
            continue
        cluster = [assets[i]]
        assigned.add(i)
        for j in range(i + 1, n):
            if j in assigned:
                continue
            # j gehört zu cluster, wenn Korrelation zu IRGENDEINEM Mitglied ≥ threshold
            if any(matrix[i][j] >= threshold for _ in [None]):
                if matrix[i][j] >= threshold:
                    cluster.append(assets[j])
                    assigned.add(j)
        clusters.append(cluster)
    return clusters


def main():
    parser = argparse.ArgumentParser(description="Asset-Korrelations-Check")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--from",   dest="start", default="2025-04-21")
    parser.add_argument("--to",     dest="end",   default="2026-04-19")
    parser.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args()

    assets = [a.strip().upper() for a in args.assets.split(",")]
    print(f"📈 Asset-Korrelations-Check")
    print(f"   Assets: {', '.join(assets)}")
    print(f"   Periode: {args.start} → {args.end}")
    print(f"   Redundanz-Threshold: r ≥ {args.threshold}")

    matrix, n_days = build_matrix(assets, args.start, args.end)
    print(f"   Gemeinsame Tage: {n_days}")

    print_matrix(assets, matrix)

    # Paare > threshold
    print(f"\n  === Redundante Paare (r ≥ {args.threshold}) ===")
    redundant = []
    for i in range(len(assets)):
        for j in range(i + 1, len(assets)):
            if matrix[i][j] >= args.threshold:
                redundant.append((assets[i], assets[j], matrix[i][j]))
    if not redundant:
        print(f"  (keine Paare über {args.threshold})")
    else:
        for a, b, r in sorted(redundant, key=lambda x: -x[2]):
            print(f"    {a:<5} vs {b:<5}  r={r:+.3f}")

    # Stark entkoppelte Paare (< 0.6) — Kandidaten für Diversifikation
    print(f"\n  === Am schwächsten korrelierte Paare (r < 0.70) ===")
    low = []
    for i in range(len(assets)):
        for j in range(i + 1, len(assets)):
            if matrix[i][j] < 0.70:
                low.append((assets[i], assets[j], matrix[i][j]))
    if not low:
        print(f"  (alle Paare r ≥ 0.70 — Markt stark gekoppelt)")
    else:
        for a, b, r in sorted(low, key=lambda x: x[2]):
            print(f"    {a:<5} vs {b:<5}  r={r:+.3f}")

    # Mittlere Korrelation pro Asset (Dominanz-Proxy)
    print(f"\n  === Mittlere Korrelation pro Asset (höher = weniger diversifikativ) ===")
    mean_corr = []
    for i, a in enumerate(assets):
        others = [matrix[i][j] for j in range(len(assets)) if j != i]
        mean_corr.append((a, sum(others) / len(others)))
    for a, mc in sorted(mean_corr, key=lambda x: -x[1]):
        bar = "█" * int(mc * 40)
        print(f"  {a:<5}  r̄={mc:+.3f}  {bar}")


if __name__ == "__main__":
    main()
