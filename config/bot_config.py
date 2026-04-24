"""
APEX Bot Konfiguration
Zentrale Einstellungen – hier alles anpassen.
"""

# MODUS

# DRY_RUN=True  → kein echtes Geld, nur Simulation (zum Testen)
# DRY_RUN=False → Live-Trading mit echtem Geld
DRY_RUN = True

# KAPITAL & RISIKO

CAPITAL = 68.33         # Startkapital in USDT (Andre, Live-Start 30.03.2026)
MAX_RISK_PCT = 0.02     # Max Risiko pro Trade (2%)
MIN_RR_RATIO = 2.0      # Mindest Risk/Reward Verhältnis
DRAWDOWN_KILL_PCT = 0.50  # Kill-Switch: keine Trades wenn Balance < 50% von CAPITAL

# PRE-TRADE SANITY CHECK (Opt 1)
MIN_BALANCE_USD = 10.0       # Keine Trades unter $10 Balance (Min-Order-Risiko)
MAX_SL_DISTANCE_PCT = 0.10   # SL-Abstand > 10% vom Entry → implausibel, kein Trade

# DAILY DRAWDOWN CIRCUIT BREAKER — Graduated (IDEA-006)
# Stufe 1: daily_r <= -1.5R → Risk × 0.5 (halbe Größe, Trade läuft noch)
# Stufe 2: daily_r <= -2.0R → Kein neuer Trade heute (KILL)
DAILY_DD_HALF_R  = -1.5      # H-016: Halbe Size ab diesem Tages-R
DAILY_DD_KILL_R  = -2.0      # Kein Trade ab diesem Tages-R (Auto-Reset 00:00 Berlin)

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
H014_VOLUME_RATIO_MIN      = 2.0   # Mindest-Volume gegenüber 20er-Avg (erhöht von 1.0 → 2026-04-24, alle Losses bei Vol<2.0x)

# H-015 · Regime-basierter Risk-Modifier (Phase B.1)
# regime_detector.py liefert risk_modifier in [0.0, 1.0]. Effektiver Risk% pro Trade
# = MAX_RISK_PCT × risk_modifier. Bei regime="crash" (risk_modifier=0) → NO-TRADE.
# Fail-safe: Regime-Detect-Fehler → Modifier=1.0 (voll), Trade läuft durch.
H015_REGIME_RISK_MODIFIER_ENABLED = True

# WEEKEND MOMENTUM

WEEKEND_ASSET = "AVAX"
MOMENTUM_THRESHOLD = 0.03   # 3% Mindest-Momentum
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0     # = 2:1 R:R

# ─── VAA STRATEGIE (Volume Absorption Anomaly) ───────────────────────────────
# Validiert: Phase 4+5, 11/11 Hard-Gates bestanden (2026-04-22)
# OOS: AvgR=+1.47R, WR=64%, PF=4.63, DSR=0.97

VAA_ENABLED      = True          # False = VAA-Bot deaktiviert
VAA_DRY_RUN      = True          # Separate DRY_RUN-Kontrolle für VAA (Start: True)
VAA_ASSETS       = ["SOL", "AVAX", "DOGE", "ADA", "SUI", "AAVE"]
VAA_BLACKLIST    = ["ETH", "LINK", "BTC", "XRP", "BNB", "OP", "ARB", "INJ",
                    "APT", "TIA", "WIF", "PEPE", "JUP", "LDO"]  # Phase-4 + Asset-Scan 2026-04-24

VAA_VOL_MULT     = 2.5           # Volumen > 2.5 × Vol_SMA(50)
VAA_BODY_MULT    = 0.6           # Kerzenkörper < 0.6 × Body_SMA(50)
VAA_ATR_EXPAND   = 1.2           # ATR(14) > 1.2 × ATR_SMA(20)  [F-06]
VAA_TP_R         = 3.0           # Take-Profit in R
VAA_ENTRY_WINDOW = 3             # Sell-Stop gültig für N Stunden nach Signal

VAA_VOL_SMA_PERIOD  = 50
VAA_BODY_SMA_PERIOD = 50
VAA_EMA_PERIOD      = 20
VAA_ATR_PERIOD      = 14
VAA_CANDLE_LIMIT    = 120        # 1H-Candles für Indikator-Berechnung (5 Tage Warmup)

# Risiko: gleiche 2% wie ORB, aber separater Daily-DD-Zähler
VAA_MAX_RISK_PCT = 0.02

# ─── KDT STRATEGIE (Kinetic Deceleration Trap) ────────────────────────────────
# Validierungsstand: Phase 4+5, 4/6 Hard-Gates (DSR + Bootstrap offen wegen n=17)
# IS  : n=17  AvgR=+0.450R  WR=41%  PF=1.64  (2025-04-21→2026-02-10)
# OOS : n=4   AvgR=+0.824R  WR=50%  PF=2.48  (2026-02-11→2026-04-19)
# → Forward-Testing bis n≥30 Live-Signale für finale Validierung
# → ETH SHORT-only: kinetische Erschöpfung nach 3 grünen Kerzen über EMA(50)

KDT_ENABLED      = True          # False = KDT-Bot deaktiviert
KDT_DRY_RUN      = True          # Start: DRY RUN — live erst nach n≥10 Signalen ohne Anomalie
KDT_ASSET        = "ETH"         # Phase-3-KEEP: einziges Asset das alle Kriterien erfüllt

KDT_EMA_PERIOD   = 50            # Phase-1-Gewinner: EMA(50) als Trend-Kontext
KDT_ENTRY_WINDOW = 2             # Sell-Stop gültig für N Stunden nach Signal
KDT_TP_R         = 3.0           # Take-Profit in R (Phase-1-Gewinner)
KDT_SL_ATR_MULT  = 1.0           # F-04 Tight-SL: SL-Distanz < k × ATR(14)

KDT_CANDLE_LIMIT = 120           # 1H-Candles für Indikator-Berechnung (≥55 für EMA(50)-Warmup)
KDT_MAX_RISK_PCT = 0.02          # 2% Risiko pro Trade (separater DD-Zähler)

# PFADE

import os
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
