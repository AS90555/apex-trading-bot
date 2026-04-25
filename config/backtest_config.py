"""
Backtest-Konfiguration für Phase 1+ Walk-Forward Analysis.

HEILIGES PRINZIP: OOS-Daten werden NIE zur Optimierung herangezogen.
Erster und einziger Blick am Ende von Phase 5.
"""

# ─── IS/OOS-Split ────────────────────────────────────────────────────────────
DATA_START = "2025-04-21"  # Anfang historische Daten
DATA_END   = "2026-04-19"  # Ende historische Daten

IS_START   = "2025-04-21"
IS_END     = "2026-02-10"  # 10 Monate In-Sample (~1.450 Trades)

OOS_START  = "2026-02-11"
OOS_END    = "2026-04-19"  # 2 Monate Out-of-Sample (~300 Trades) — LOCKED bis Phase 5

# ─── Walk-Forward-Parameter ──────────────────────────────────────────────────
WFA_IS_MONTHS   = 6   # IS-Fenster pro Fold
WFA_OOS_MONTHS  = 1   # OOS-Fenster pro Fold
WFA_STEP_MONTHS = 1   # Schritt zwischen Folds
WFA_MIN_FOLDS_POSITIVE = 4  # Strategie muss in ≥4 von 6-7 Folds OOS-profitabel sein
WFA_ACCEPT_WFE = 0.5  # Walk-Forward-Efficiency-Schwelle (Pardo-Standard)

# ─── Monte Carlo ─────────────────────────────────────────────────────────────
MC_ITERATIONS       = 10_000
MC_SEED             = 42
MC_LOWER_PERCENTILE = 5   # 5-Perzentil Final-Equity muss > 0 sein
MC_UPPER_PERCENTILE = 95  # Max-DD muss innerhalb 95-Perzentil liegen

# ─── Bonferroni-Korrektur ────────────────────────────────────────────────────
BONFERRONI_ALPHA    = 0.05
# α_adjusted = BONFERRONI_ALPHA / n_tests

# ─── Deflated Sharpe Ratio (López de Prado) ──────────────────────────────────
DSR_ACCEPT_THRESHOLD = 0.5  # DSR > 0.5 = echter Edge
DSR_NOISE_THRESHOLD  = 0.3  # DSR < 0.3 = wahrscheinlich Noise

# ─── Risk-Per-Trade Annahmen (für Equity-Simulation) ─────────────────────────
# Bei 50 USDT Startkapital und 2% Risk = $1 pro Trade. Equity-Kurve normiert auf R.
START_EQUITY_R = 0.0

# ─── Acceptance-Gates Phase 5 ────────────────────────────────────────────────
GATE_MIN_OOS_AVG_R    = 0.05
GATE_MIN_OOS_PF       = 1.4
GATE_MIN_OOS_SHARPE   = 1.0  # annualisiert
GATE_MIN_ASSETS       = 3
GATE_MAX_NOISE_DEGRAD = 0.30  # Noise-Injection darf Sharpe max. 30% degradieren
