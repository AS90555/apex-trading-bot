"""
APEX Bot Konfiguration
======================
Zentrale Einstellungen – hier alles anpassen.
"""

# ========================
# MODUS
# ========================

# DRY_RUN=True  → kein echtes Geld, nur Simulation (zum Testen)
# DRY_RUN=False → Live-Trading mit echtem Geld
DRY_RUN = True

# ========================
# KAPITAL & RISIKO
# ========================

CAPITAL = 50.0          # Startkapital in USDT
MAX_RISK_PCT = 0.02     # Max Risiko pro Trade (2%)
MIN_RR_RATIO = 2.0      # Mindest Risk/Reward Verhältnis
DRAWDOWN_KILL_PCT = 0.50  # Kill-Switch: keine Trades wenn Balance < 50% von CAPITAL

# ========================
# HEBEL
# ========================

LEVERAGE = 5            # Hebel (5x empfohlen bei 50 USDT)
                        # Nur für Mindestordergröße nötig – Risk bleibt 2%

# ========================
# TRADING ASSETS
# ========================

# BTC bei 50 USDT nicht handelbar (Mindestorder zu groß)
# → ETH, SOL, AVAX verwenden
ASSETS = ["ETH", "SOL", "AVAX"]

# Asset-Priorität für Breakout-Auswahl (höchste zuerst)
ASSET_PRIORITY = ["ETH", "SOL", "AVAX"]

# Mindest-Dezimalstellen je Asset (Bitget szDecimals)
SIZE_DECIMALS = {
    "BTC":  3,
    "ETH":  2,
    "SOL":  1,
    "AVAX": 0,
}

# ========================
# ORB STRATEGIE
# ========================

MAX_SPREAD_PCT = 0.1    # Maximaler Spread in % (Validierungskriterium 5)
BREAKOUT_THRESHOLD = {  # Mindestdistanz für Breakout-Erkennung
    "ETH":  5.0,        # $5 über/unter Box
    "SOL":  0.30,       # $0.30 über/unter Box
    "AVAX": 0.15,       # $0.15 über/unter Box
}

# ========================
# WEEKEND MOMENTUM
# ========================

WEEKEND_ASSET = "AVAX"
MOMENTUM_THRESHOLD = 0.03   # 3% Mindest-Momentum
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0     # = 2:1 R:R

# ========================
# PFADE
# ========================

import os
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
