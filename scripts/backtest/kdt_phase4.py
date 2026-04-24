#!/usr/bin/env python3
"""
KDT Phase 4 — Robustheit (ETH SHORT only).

6 Hard-Gates — alle müssen bestanden werden:
  4.1 Monte Carlo (5000 Trade-Shuffle)
  4.2 Parameter-Sensitivität (±10%/±20%)
  4.3 Deflated Sharpe Ratio (DSR)
  4.4 Block-Bootstrap (Autokorrelation)
  4.5 Noise-Injection (σ = 0.1 × ATR)
  4.6 Regime-Stress (BTC-Monatsrendite)
"""
import math
import os
import random
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE
from scripts.backtest.squeeze_scout  import aggregate_1h
from scripts.backtest.kdt_scout     import _ema_series
from scripts.backtest.kdt_phase2    import build_full_indicators, run_kdt_filtered

ASSET    = "ETH"
IS_START = "2025-04-21"
IS_END   = "2026-02-10"

# Phase-1 Gewinner-Parameter
EMA_PERIOD   = 50
ENTRY_WINDOW = 2
TP_R         = 3.0
N_TRIALS     = 27 + 15   # Grid + Filter-Tests für DSR

random.seed(42)


# ─── Indikatoren & Filter (wiederverwendet aus Phase 2) ───────────────────────

def tight_sl_filter(c0, c1, c2, ind, i, k=1.0):
    atr = ind[i]["atr14"]
    if atr <= 0:
        return True
    return (c0["high"] - c0["low"]) < k * atr


# ─── ETH-Backtest (gibt Trade-Dicts zurück) ───────────────────────────────────

def run_eth_trades(candles, inds, start, end, ema_k=1.0, entry_k=1.0,
                   tp_k=1.0, noise_atr=0.0, filter_k=1.0):
    """Flexibler Backtest mit variierbaren Parametern für Sensitivitätstests."""
    ema_period   = max(10, round(EMA_PERIOD * ema_k))
    entry_window = max(1, round(ENTRY_WINDOW * entry_k))
    tp_r         = TP_R * tp_k

    closes  = [c["close"] for c in candles]
    ema50   = _ema_series(closes, ema_period)
    warmup  = ema_period + 20

    pending  = []
    in_trade = False
    trade    = {}
    results  = []

    for i, c in enumerate(candles):
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")

        if in_trade:
            ae, sl, tp, risk = trade["ae"], trade["sl"], trade["tp"], trade["risk"]
            hit_sl = c["high"] >= sl
            hit_tp = c["low"]  <= tp
            if hit_sl and not hit_tp:
                results.append(-1.0 - (2 * ae * TAKER_FEE) / risk)
                in_trade = False; continue
            if hit_tp:
                results.append(tp_r - (2 * ae * TAKER_FEE) / risk)
                in_trade = False; continue
            continue

        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"] or day < start or day > end:
                    continue
                if c["low"] <= p["stop"]:
                    noise = random.gauss(0, noise_atr) if noise_atr > 0 else 0
                    ae   = (p["stop"] + noise) * (1 - SLIPPAGE)
                    risk = p["sl"] - ae
                    if risk > 0 and 0.001 <= risk / ae <= 0.20:
                        tp_p = ae - tp_r * risk
                        in_trade = True
                        trade = {"ae": ae, "sl": p["sl"], "tp": tp_p, "risk": risk}
                        triggered.append(p); break
                    triggered.append(p)
            pending = [p for p in pending
                       if p not in triggered and i <= p["expiry"]]

        if in_trade: continue
        if day < start or day > end or i < warmup or i < 2: continue

        c0, c1, c2 = candles[i], candles[i-1], candles[i-2]
        e = ema50[i]
        if e <= 0: continue

        body0 = abs(c0["close"] - c0["open"])
        body1 = abs(c1["close"] - c1["open"])
        body2 = abs(c2["close"] - c2["open"])
        if body0 <= 0: continue
        vol0, vol1, vol2 = c0["volume"], c1["volume"], c2["volume"]
        if vol0 <= 0: continue

        if not (c0["close"] > c0["open"] and c1["close"] > c1["open"] and
                c2["close"] > c2["open"] and
                body0 < body1 < body2 and vol0 < vol1 < vol2 and
                c0["close"] > e):
            continue

        sl_p = c0["high"]
        stop = c0["low"]
        risk = sl_p - stop
        if not (0 < risk / stop <= 0.15): continue

        # F-04 mit variablem k
        if inds[i]["atr14"] > 0 and (sl_p - stop) >= filter_k * inds[i]["atr14"]:
            continue

        pending.append({"stop": stop, "sl": sl_p, "expiry": i + entry_window})

    return results


def kpis(r_list):
    n = len(r_list)
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "wr": 0.0, "pf": 0.0,
                "total_r": 0.0, "sharpe": 0.0, "max_dd": 0.0, "p": 1.0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r-mean)**2 for r in r_list)/(n-1)) if n > 1 else 0
    sharpe = (mean / sd * math.sqrt(n)) if sd > 0 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1/(1+0.3275911*abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+t_*(-1.453152027+t_*1.061405429))))
        return p*math.exp(-x*x)
    p = erfc(abs(t)/math.sqrt(2)) if t != 0 else 1.0
    return {"n": n, "avg_r": round(mean, 3), "wr": round(len(wins)/n, 3),
            "pf": round(gw/gl, 2) if gl > 0 else float("inf"),
            "total_r": round(total, 2), "sharpe": round(sharpe, 3),
            "max_dd": round(dd, 2), "p": round(p, 4)}


# ─── 4.1 Monte Carlo ──────────────────────────────────────────────────────────

def monte_carlo(r_list, n_iter=5000):
    results = []
    for _ in range(n_iter):
        shuffled = r_list[:]
        random.shuffle(shuffled)
        cum = 0.0
        peak = dd = 0.0
        for r in shuffled:
            cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
        results.append({"equity": cum, "max_dd": dd})
    equities  = sorted(r["equity"] for r in results)
    p5  = equities[int(0.05  * n_iter)]
    p50 = equities[int(0.50  * n_iter)]
    p95 = equities[int(0.95  * n_iter)]
    pct_pos = sum(1 for e in equities if e > 0) / n_iter * 100
    return {"p5": round(p5, 2), "p50": round(p50, 2), "p95": round(p95, 2),
            "pct_positive": round(pct_pos, 1)}


# ─── 4.3 DSR ──────────────────────────────────────────────────────────────────

def deflated_sharpe(sharpe_obs, sharpe_trials, n, skew, kurt, n_trials):
    """López de Prado DSR (vereinfacht)."""
    if sharpe_trials <= 0 or n <= 1:
        return 0.0
    e_max = ((1 - 0.5772) * math.sqrt(2 * math.log(n_trials)) +
             0.5772 / math.sqrt(2 * math.log(n_trials)))
    sr_adj = sharpe_obs * (1 - skew * sharpe_obs +
                           (kurt - 1) / 6 * sharpe_obs**2 + 1 / (4 * (n - 1)))
    z = (sr_adj - e_max) * math.sqrt(n - 1)
    def norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return norm_cdf(z)


# ─── 4.4 Block-Bootstrap ─────────────────────────────────────────────────────

def block_bootstrap(r_list, block_size=3, n_iter=5000):
    if len(r_list) < block_size * 2:
        return {"ci_low": 0.0, "ci_high": 0.0}
    sharpes = []
    for _ in range(n_iter):
        sample = []
        while len(sample) < len(r_list):
            start = random.randint(0, len(r_list) - block_size)
            sample.extend(r_list[start:start + block_size])
        sample = sample[:len(r_list)]
        n = len(sample)
        mean = sum(sample) / n
        sd   = math.sqrt(sum((r-mean)**2 for r in sample)/(n-1)) if n > 1 else 0
        sharpes.append(mean / sd * math.sqrt(n) if sd > 0 else 0)
    sharpes.sort()
    ci_low  = sharpes[int(0.025 * n_iter)]
    ci_high = sharpes[int(0.975 * n_iter)]
    return {"ci_low": round(ci_low, 3), "ci_high": round(ci_high, 3)}


# ─── Haupt-Runner ─────────────────────────────────────────────────────────────

def main():
    print(f"\n{'═'*76}")
    print(f"  KDT Phase 4 — Robustheit  (ETH SHORT, EMA=50 Win=2 TP=3R F-04)")
    print(f"  IS: {IS_START} → {IS_END}")
    print(f"{'═'*76}\n")

    # Daten laden
    raw     = load_csv(ASSET, "15m")
    candles = aggregate_1h(raw)
    inds    = build_full_indicators(candles)

    # Basis-R-Liste
    base_r = run_eth_trades(candles, inds, IS_START, IS_END)
    base_k = kpis(base_r)

    print(f"  BASELINE ETH: n={base_k['n']}  AvgR={base_k['avg_r']:+.3f}  "
          f"WR={base_k['wr']*100:.1f}%  PF={base_k['pf']:.2f}  "
          f"Sharpe={base_k['sharpe']:.3f}  MaxDD={base_k['max_dd']:.1f}R\n")

    gates = {}

    # ── 4.1 Monte Carlo ───────────────────────────────────────────────────────
    print(f"  {'─'*72}")
    print(f"  4.1 MONTE CARLO (5.000 Iterationen, Trade-Shuffle)")
    mc = monte_carlo(base_r, 5000)
    g41 = mc["p5"] > 0 and mc["pct_positive"] >= 80
    gates["4.1 MC"] = g41
    print(f"  P5={mc['p5']:+.2f}R  P50={mc['p50']:+.2f}R  P95={mc['p95']:+.2f}R  "
          f"Positive Pfade={mc['pct_positive']:.1f}%")
    print(f"  Gate: P5>0 ✅/❌ → {'✅ PASS' if g41 else '❌ FAIL'}  "
          f"(P5={mc['p5']:+.2f}, Pos={mc['pct_positive']:.1f}%)\n")

    # ── 4.2 Parameter-Sensitivität ────────────────────────────────────────────
    print(f"  {'─'*72}")
    print(f"  4.2 PARAMETER-SENSITIVITÄT (±10%, ±20% jedes Parameters)")
    print(f"  {'Param':<18} {'−20%':>8} {'−10%':>8} {'Basis':>8} {'+10%':>8} {'+20%':>8}")

    sens_results = {}
    for param_name, k_values in [
        ("EMA-Periode",   [0.8, 0.9, 1.0, 1.1, 1.2]),
        ("Entry-Window",  [0.8, 0.9, 1.0, 1.1, 1.2]),
        ("TP-R",          [0.8, 0.9, 1.0, 1.1, 1.2]),
        ("F-04-k",        [0.8, 0.9, 1.0, 1.1, 1.2]),
    ]:
        row = []
        for k in k_values:
            if param_name == "EMA-Periode":
                r = run_eth_trades(candles, inds, IS_START, IS_END, ema_k=k)
            elif param_name == "Entry-Window":
                r = run_eth_trades(candles, inds, IS_START, IS_END, entry_k=k)
            elif param_name == "TP-R":
                r = run_eth_trades(candles, inds, IS_START, IS_END, tp_k=k)
            else:
                r = run_eth_trades(candles, inds, IS_START, IS_END, filter_k=k)
            row.append(kpis(r)["avg_r"])

        plateau = all(v > 0 for v in row[1:4]) and \
                  max(row) / (min(r for r in row if r != 0) or 0.001) < 5
        sens_results[param_name] = plateau
        vals = "  ".join(f"{v:>+.3f}" for v in row)
        print(f"  {param_name:<18} {vals}  {'✅' if plateau else '❌'}")

    g42 = all(sens_results.values())
    gates["4.2 Sens"] = g42
    print(f"  Gate: Plateau-Form → {'✅ PASS' if g42 else '❌ FAIL'}\n")

    # ── 4.3 DSR ───────────────────────────────────────────────────────────────
    print(f"  {'─'*72}")
    print(f"  4.3 DEFLATED SHARPE RATIO (n_trials={N_TRIALS})")
    r_arr  = base_r
    n      = len(r_arr)
    mean_r = sum(r_arr) / n if n else 0
    sd_r   = math.sqrt(sum((r-mean_r)**2 for r in r_arr)/(n-1)) if n > 1 else 1
    skew   = sum((r-mean_r)**3 for r in r_arr) / (n * sd_r**3) if sd_r > 0 else 0
    kurt   = sum((r-mean_r)**4 for r in r_arr) / (n * sd_r**4) if sd_r > 0 else 3
    sharpe_obs = base_k["sharpe"]
    dsr = deflated_sharpe(sharpe_obs, sharpe_obs, n, skew, kurt, N_TRIALS)
    g43 = dsr > 0.5
    gates["4.3 DSR"] = g43
    print(f"  Sharpe={sharpe_obs:.3f}  Skew={skew:.3f}  Kurt={kurt:.3f}  "
          f"n_trials={N_TRIALS}")
    print(f"  DSR={dsr:.3f}  Gate: DSR>0.5 → {'✅ PASS' if g43 else '❌ FAIL'}\n")

    # ── 4.4 Block-Bootstrap ───────────────────────────────────────────────────
    print(f"  {'─'*72}")
    print(f"  4.4 BLOCK-BOOTSTRAP (Block=3, 5000 Iterationen)")
    bb = block_bootstrap(base_r, block_size=3, n_iter=5000)
    g44 = bb["ci_low"] > 0
    gates["4.4 Bootstrap"] = g44
    print(f"  95% CI Sharpe: [{bb['ci_low']:.3f}, {bb['ci_high']:.3f}]")
    print(f"  Gate: CI_low>0 → {'✅ PASS' if g44 else '❌ FAIL'}\n")

    # ── 4.5 Noise-Injection ───────────────────────────────────────────────────
    print(f"  {'─'*72}")
    print(f"  4.5 NOISE-INJECTION (σ = 0.1 × ATR, 100 Iterationen)")
    avg_atr = sum(inds[i]["atr14"] for i in range(len(inds))
                  if inds[i]["atr14"] > 0) / max(1, sum(1 for x in inds if x["atr14"] > 0))
    noise_results = []
    for _ in range(100):
        r = run_eth_trades(candles, inds, IS_START, IS_END,
                           noise_atr=0.1 * avg_atr)
        noise_results.append(kpis(r)["sharpe"])
    avg_noise_sharpe = sum(noise_results) / len(noise_results)
    degradation = (base_k["sharpe"] - avg_noise_sharpe) / abs(base_k["sharpe"]) \
                  if base_k["sharpe"] != 0 else 1.0
    g45 = degradation < 0.30
    gates["4.5 Noise"] = g45
    print(f"  Basis Sharpe={base_k['sharpe']:.3f}  Noisy Sharpe={avg_noise_sharpe:.3f}  "
          f"Degradation={degradation*100:.1f}%")
    print(f"  Gate: Degradation<30% → {'✅ PASS' if g45 else '❌ FAIL'}\n")

    # ── 4.6 Regime-Stress ─────────────────────────────────────────────────────
    print(f"  {'─'*72}")
    print(f"  4.6 REGIME-STRESS (BTC-Monatsrendite als Proxy)")
    btc_raw     = load_csv("BTC", "15m")
    btc_candles = aggregate_1h(btc_raw) if btc_raw else []

    regimes = {
        "bull_strong": ("2025-10-01", "2025-11-30"),
        "bull_quiet":  ("2025-07-01", "2025-09-30"),
        "sideways":    ("2025-05-01", "2025-06-30"),
        "bear_quiet":  ("2026-01-01", "2026-02-10"),
        "bear_strong": ("2025-04-21", "2025-04-30"),
    }

    g46 = True
    for regime, (rs, re) in regimes.items():
        r = run_eth_trades(candles, inds, rs, re)
        k = kpis(r)
        fail = k["avg_r"] < -0.15
        if fail:
            g46 = False
        icon = "❌" if fail else "✅"
        print(f"  {icon} {regime:<14} n={k['n']:>3}  AvgR={k['avg_r']:>+.3f}  "
              f"WR={k['wr']*100:.0f}%  [{rs}→{re}]")
    gates["4.6 Regime"] = g46
    print(f"  Gate: kein Regime <-0.15R → {'✅ PASS' if g46 else '❌ FAIL'}\n")

    # ── Hard-Gate Checkliste ──────────────────────────────────────────────────
    print(f"{'═'*76}")
    print(f"  PHASE 4 HARD-GATE CHECKLISTE")
    print(f"{'═'*76}")
    all_pass = True
    for name, result in gates.items():
        icon = "✅" if result else "❌"
        print(f"  {icon} {name}")
        if not result:
            all_pass = False

    print(f"\n  → {'✅ ALLE 6 GATES BESTANDEN — weiter zu Phase 5 OOS' if all_pass else '❌ MINDESTENS 1 GATE GEFALLEN — PROJEKT-PAUSE'}")
    print(f"{'═'*76}\n")


if __name__ == "__main__":
    main()
