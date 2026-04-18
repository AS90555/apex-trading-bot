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

# PRE-TRADE SANITY CHECK (Opt 1)
MIN_BALANCE_USD = 10.0       # Keine Trades unter $10 Balance (Min-Order-Risiko)
MAX_SL_DISTANCE_PCT = 0.10   # SL-Abstand > 10% vom Entry → implausibel, kein Trade

# DAILY DRAWDOWN CIRCUIT BREAKER (Opt 2)
DAILY_DD_KILL_R = -2.0       # Tages-R-Limit: bei <= -2R heute keine neuen Trades mehr
                              # Auto-Reset um 00:00 Berlin (Datumswechsel)

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
    "ETH":  3.0,        # $3 über/unter Box (reduziert von $5 — war 42–125% der Box-Range → H-003)
    "SOL":  0.10,       # $0.10 über/unter Box (reduziert von $0.30 — war 250% der Box-Range)
    "AVAX": 0.03,       # $0.03 über/unter Box (reduziert von $0.05 — war 100–125% der Box-Range → H-004)
    "XRP":  0.001,      # $0.001 über/unter Box (war $0.005 = 111% der Box-Range → nie getradet)
}
MIN_BOX_RANGE = {       # Mindest-Kerzenbreite für gültige ORB-Box (15m-Candle)
    "ETH":  1.0,        # $1.00 – unter 0.05% Range ist kein ORB
    "SOL":  0.10,       # $0.10
    "AVAX": 0.04,       # $0.04
    "XRP":  0.003,      # $0.003
}
MAX_BOX_AGE_MIN = 120   # Box maximal 2h alt (verhindert Vortagsdaten)
MAX_BREAKOUT_DISTANCE_RATIO = 2.0  # Max Breakout-Distanz als Vielfaches der Box-Range
                                    # Verhindert Chasing: Preis > 2x Range über Box-Grenze → kein Trade
                                    # Beispiel ETH Box $9.23: max $18.46 über Box-High erlaubt

# H-006: EMA-200 Alignment Filter (aktiviert 2026-04-16 nach 12-Trade-Analyse)
# Datenbasis: 12 Trades mit trend_context. MIT-Trend: Avg +0.09R, WR 50% (n=4).
# GEGEN-Trend: Avg −0.80R, WR 12% (n=8). Filter hätte 7 von 8 Gegen-Trend-Losses
# vermieden bei Kosten von 1 BE_WIN → Netto +6.44R auf 12 Trades.
# Fail-safe: Fehlt ema_200 oder ist EMA-Daten-Berechnung fehlgeschlagen → Trade läuft
# wie bisher durch (kein Blockieren bei unklarer Datenlage).
H006_EMA_FILTER_ENABLED = True
H006_REQUIRE_H4_ALIGN   = True   # True = zusätzlich 4H-EMA-50-Alignment erforderlich

# H-014 · Volume-Ratio Filter (Skip bei schwachem Breakout-Volumen)
# Datenbasis: 21 Trades mit volume_ratio. Vol<1.0: Avg −0.487R, WR 38% (n=8).
# Vol≥1.0: Avg −0.208R, WR 46% (n=13). Filter hätte 8 Skip-Trades für +3.90R vermieden.
# Fail-safe: Fehlt volume_ratio oder Candle-API fail → Trade läuft durch (kein Block).
H014_VOLUME_FILTER_ENABLED = True
H014_VOLUME_RATIO_MIN      = 1.0   # Mindest-Volume gegenüber 20er-Avg

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
