"""
APEX Bot Konfiguration
Zentrale Einstellungen – hier alles anpassen.
"""

# MODUS

# DRY_RUN=True  → kein echtes Geld, nur Simulation (zum Testen)
# DRY_RUN=False → Live-Trading mit echtem Geld
DRY_RUN = False

# KAPITAL & RISIKO

CAPITAL = 68.33         # Startkapital in USDT (Andre, Live-Start 30.03.2026)
MAX_RISK_PCT = 0.02     # Max Risiko pro Trade (2%)
MIN_RR_RATIO = 2.0      # Mindest Risk/Reward Verhältnis
DRAWDOWN_KILL_PCT = 0.50  # Kill-Switch: keine Trades wenn Balance < 50% von CAPITAL

# HEBEL

LEVERAGE = 5            # Hebel (5x empfohlen bei 50 USDT)
                        # Nur für Mindestordergröße nötig – Risk bleibt 2%

MARGIN_MODE = "isolated"  # "isolated" (sicherer, klein Konto) oder "crossed"

# TRADING ASSETS

# BTC bei 50 USDT nicht handelbar (Mindestorder zu groß)
# → ETH, SOL, AVAX verwenden
ASSETS = ["ETH", "SOL", "AVAX", "XRP"]

# Asset-Priorität für Breakout-Auswahl (höchste zuerst)
ASSET_PRIORITY = ["ETH", "SOL", "AVAX", "XRP"]

# Dezimalstellen für Positionsgröße (Bitget volumePlace)
SIZE_DECIMALS = {
    "BTC":  4,
    "ETH":  2,
    "SOL":  1,
    "AVAX": 1,
    "XRP":  0,
}

# Dezimalstellen für Preise (Bitget pricePlace)
PRICE_DECIMALS = {
    "BTC":  1,
    "ETH":  2,
    "SOL":  3,
    "AVAX": 3,
    "XRP":  4,
}

# ORB STRATEGIE

MAX_SPREAD_PCT = 0.1    # Maximaler Spread in % (Validierungskriterium 5)
BREAKOUT_THRESHOLD = {  # Mindestdistanz für Breakout-Erkennung
    "ETH":  5.0,        # $5 über/unter Box
    "SOL":  0.10,       # $0.10 über/unter Box (reduziert von $0.30 — war 250% der Box-Range)
    "AVAX": 0.05,       # $0.05 über/unter Box (reduziert von $0.15 — war 250% der Box-Range)
    "XRP":  0.005,      # $0.005 über/unter Box
}
MIN_BOX_RANGE = {       # Mindest-Kerzenbreite für gültige ORB-Box (15m-Candle)
    "ETH":  1.0,        # $1.00 – unter 0.05% Range ist kein ORB
    "SOL":  0.10,       # $0.10
    "AVAX": 0.04,       # $0.04
    "XRP":  0.003,      # $0.003
}
MAX_BOX_AGE_MIN = 120   # Box maximal 2h alt (verhindert Vortagsdaten)

# WEEKEND MOMENTUM

WEEKEND_ASSET = "AVAX"
MOMENTUM_THRESHOLD = 0.03   # 3% Mindest-Momentum
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0     # = 2:1 R:R

# PFADE

import os
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
