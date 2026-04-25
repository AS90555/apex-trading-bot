#!/usr/bin/env python3
"""
Phase 1.3/1.5 — Hypothesis Tester mit Bonferroni-Korrektur + Deflated Sharpe Ratio.

Aufgaben:
  - Registriere Tests (hypothesis_id, parameter_values, expected, null_hypothesis)
  - t-Test auf Mittelwert R (H0: mean_r = 0)
  - Bonferroni: α_adjusted = α / n_tests
  - Deflated Sharpe Ratio (López de Prado 2014)

Als Modul:
  from scripts.backtest.hypothesis_tester import HypothesisTester
  tester = HypothesisTester(alpha=0.05)
  tester.register("H-200_tp05", r_list, expected_effect="mean_r > 0")
  ...
  tester.evaluate()
"""
import math


def t_statistic(r_list: list[float], h0_mean: float = 0.0) -> tuple[float, float]:
    """Returns (t_stat, df). p-Wert via Approximation in p_value_two_tailed()."""
    n = len(r_list)
    if n < 2:
        return 0.0, 0
    mean = sum(r_list) / n
    var  = sum((r - mean) ** 2 for r in r_list) / (n - 1)
    sd   = math.sqrt(var)
    se   = sd / math.sqrt(n)
    if se == 0:
        return 0.0, n - 1
    return (mean - h0_mean) / se, n - 1


def p_value_two_tailed(t: float, df: int) -> float:
    """
    Approximation via Normal-Verteilung (df > 30 → Normal ist Good Enough).
    Abraham-Stegun 26.2.17 Approximation für CDF.
    """
    if df < 1:
        return 1.0
    # Für df ≥ 30 ist t-Verteilung ≈ Normal
    z = abs(t)
    # CDF-Approximation Normal
    # erfc via Taylor ist präzise genug für p ∈ [1e-10, 1]
    a1 =  0.254829592
    a2 = -0.284496736
    a3 =  1.421413741
    a4 = -1.453152027
    a5 =  1.061405429
    p_const = 0.3275911
    sign = 1 if z >= 0 else -1
    zz = abs(z) / math.sqrt(2)
    t_ = 1.0 / (1.0 + p_const * zz)
    y  = 1.0 - (((((a5 * t_ + a4) * t_) + a3) * t_ + a2) * t_ + a1) * t_ * math.exp(-zz * zz)
    erf = sign * y
    # two-tailed p
    return 2 * (1 - 0.5 * (1 + erf))


def sharpe_trade_level(r_list: list[float]) -> float:
    n = len(r_list)
    if n < 2:
        return 0.0
    mean = sum(r_list) / n
    var  = sum((r - mean) ** 2 for r in r_list) / (n - 1)
    sd   = math.sqrt(var)
    return mean / sd if sd > 0 else 0.0


def skewness(r_list: list[float]) -> float:
    n = len(r_list)
    if n < 3:
        return 0.0
    mean = sum(r_list) / n
    var  = sum((r - mean) ** 2 for r in r_list) / n
    sd   = math.sqrt(var)
    if sd == 0:
        return 0.0
    return sum(((r - mean) / sd) ** 3 for r in r_list) / n


def kurtosis(r_list: list[float]) -> float:
    """Excess kurtosis (normal = 0)."""
    n = len(r_list)
    if n < 4:
        return 0.0
    mean = sum(r_list) / n
    var  = sum((r - mean) ** 2 for r in r_list) / n
    sd   = math.sqrt(var)
    if sd == 0:
        return 0.0
    return sum(((r - mean) / sd) ** 4 for r in r_list) / n - 3


def deflated_sharpe_ratio(r_list: list[float], n_trials: int,
                          benchmark_sr: float = 0.0) -> dict:
    """
    DSR nach López de Prado 2014.
    Adjustiert observed Sharpe für:
      - Anzahl getesteter Varianten (n_trials)
      - Non-Normalität der Return-Verteilung (skew, kurtosis)

    Returns: {sr, dsr_threshold, sr_passes}
    """
    n = len(r_list)
    if n < 30 or n_trials < 1:
        return {"sr": 0.0, "dsr_threshold": 0.0, "sr_passes": False,
                "note": "insufficient data"}

    sr   = sharpe_trade_level(r_list)
    skew = skewness(r_list)
    kurt = kurtosis(r_list)

    # Euler-Mascheroni
    gamma = 0.5772156649
    # Expected Max Sharpe under null (López de Prado 2014, Eq. 7)
    # E[max SR] ≈ sqrt((1 - gamma) * Φ⁻¹(1 - 1/N) + gamma * Φ⁻¹(1 - 1/(N*e)))
    # Für unsere Zwecke approximieren wir mit der einfachen Form:
    #   E[max SR | n_trials] = sqrt(2 * ln(n_trials))
    if n_trials == 1:
        expected_max_sr = 0.0
    else:
        expected_max_sr = math.sqrt(2 * math.log(n_trials))

    # DSR-Threshold: adjustierter Sharpe, den wir mindestens brauchen
    # um mit Probabilität 1-α signifikant besser als Null zu sein.
    # Variance-Reduktion für Skew+Kurtosis:
    #   std(SR) ≈ sqrt((1 - skew*SR + (kurt-1)/4 * SR²) / (n-1))
    sr_adj_var = max((1 - skew * sr + (kurt - 1) / 4 * sr * sr) / max(n - 1, 1), 1e-9)
    std_sr = math.sqrt(sr_adj_var)

    # DSR = Φ((SR - benchmark - expected_max) / std_sr)
    # Vereinfacht: passes wenn SR > benchmark + expected_max_sr * std_sr
    dsr_threshold = benchmark_sr + expected_max_sr * std_sr
    sr_passes = sr > dsr_threshold

    return {
        "sr":             round(sr, 4),
        "expected_max":   round(expected_max_sr, 4),
        "std_sr":         round(std_sr, 4),
        "dsr_threshold":  round(dsr_threshold, 4),
        "sr_passes":      sr_passes,
        "n":              n,
        "n_trials":       n_trials,
        "skew":           round(skew, 3),
        "excess_kurt":    round(kurt, 3),
    }


class HypothesisTester:
    """
    Registriere Tests, werte mit Bonferroni aus.
    """
    def __init__(self, alpha: float = 0.05):
        self.alpha = alpha
        self.tests: list[dict] = []

    def register(self, hypothesis_id: str, r_list: list[float],
                 expected_effect: str = "mean_r != 0",
                 h0_mean: float = 0.0, notes: str = "") -> None:
        t, df = t_statistic(r_list, h0_mean)
        p = p_value_two_tailed(t, df)
        n = len(r_list)
        mean_r = sum(r_list) / n if n else 0.0
        self.tests.append({
            "id":              hypothesis_id,
            "n":               n,
            "mean_r":          mean_r,
            "t":               t,
            "p":               p,
            "expected_effect": expected_effect,
            "notes":           notes,
        })

    def evaluate(self) -> dict:
        n_tests = len(self.tests)
        if n_tests == 0:
            return {"n_tests": 0, "alpha_adj": self.alpha, "results": []}
        alpha_adj = self.alpha / n_tests
        results = []
        for t in self.tests:
            significant    = t["p"] < self.alpha
            survives_bonf  = t["p"] < alpha_adj
            if survives_bonf:
                verdict = "✅ significant (Bonferroni)"
            elif significant:
                verdict = "⚠️  overfit-verdacht (p<α aber p≥α_adj)"
            else:
                verdict = "❌ not significant"
            results.append({**t,
                            "significant":   significant,
                            "bonferroni_ok": survives_bonf,
                            "verdict":       verdict})
        return {
            "n_tests":   n_tests,
            "alpha":     self.alpha,
            "alpha_adj": alpha_adj,
            "results":   results,
        }

    def print_summary(self):
        ev = self.evaluate()
        print(f"\n  === Hypothesis-Testing (Bonferroni) ===")
        print(f"  α = {ev['alpha']:.4f}, α_adj = {ev.get('alpha_adj', 0):.5f}, "
              f"n_tests = {ev['n_tests']}")
        print(f"  {'ID':<20} {'n':>5} {'MeanR':>9} {'t':>7} {'p':>9}  Verdict")
        print(f"  {'-'*20} {'-'*5} {'-'*9} {'-'*7} {'-'*9}  {'-'*35}")
        for r in ev.get("results", []):
            print(f"  {r['id']:<20} {r['n']:>5} {r['mean_r']:>+8.3f}R "
                  f"{r['t']:>+6.2f} {r['p']:>8.5f}  {r['verdict']}")


if __name__ == "__main__":
    # Smoke-Test
    import random
    random.seed(42)
    tester = HypothesisTester(alpha=0.05)
    tester.register("H_NULL",   [random.gauss(0, 1) for _ in range(1000)])
    tester.register("H_SIGNAL", [random.gauss(0.1, 1) for _ in range(1000)])
    tester.register("H_WEAK",   [random.gauss(0.05, 1) for _ in range(1000)])
    tester.print_summary()

    print("\n  === Deflated Sharpe Ratio ===")
    strong = [random.gauss(0.05, 1) for _ in range(1000)]
    dsr = deflated_sharpe_ratio(strong, n_trials=30)
    for k, v in dsr.items():
        print(f"    {k:<16}: {v}")
