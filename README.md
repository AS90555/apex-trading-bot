# APEX Trading Bot

Vollautomatischer Krypto-Trading-Bot auf Basis der **Opening Range Breakout (ORB)**-Strategie.
Läuft cron-gesteuert auf einem Linux-Server, tradet Bitget USDT-Perpetual-Futures ohne manuelle Eingriffe.

**Exchange:** Bitget Futures | **Assets:** ETH, SOL, AVAX, XRP | **Hebel:** 5x | **Risk/Trade:** 2%

---

## Wie der Bot funktioniert

### Strategie: Opening Range Breakout (ORB)

An jedem Handelstag gibt es drei Sessions. Jede Session läuft nach demselben Schema:

```
1. Pre-Market Check   → Systemcheck (Balance, offene Positionen, API-Status)
2. Opening Range Box  → 15-Minuten-Kerze zu Session-Open wird als "Box" gespeichert
                        (High = Widerstand, Low = Unterstützung)
3. Breakout-Scan      → alle 5 Minuten: bricht Preis aus der Box aus?
4. Trade-Ausführung   → wenn alle Filter erfüllt: Market Order + SL + TP automatisch
5. Session-Summary    → Zusammenfassung nach Session-Ende
```

**Ein Trade pro Session, ein Asset pro Trade.** Assets werden in Prioritätsreihenfolge geprüft (ETH → SOL → AVAX → XRP), das erste Signal gewinnt.

### Entry-Filter (alle müssen erfüllt sein)

| # | Filter | Bedingung |
|---|--------|-----------|
| 1 | Kill-Switch | Balance > 50% Startkapital |
| 2 | Session-Lock | Noch kein Trade in dieser Session |
| 3 | Position offen | Keine offene Position für diesen Asset |
| 4 | Box-Alter | Box maximal 2 Stunden alt |
| 5 | Mindest-Box-Range | Box-Breite über Asset-Minimum |
| 6 | Breakout-Threshold | Preis bricht Min-Distanz über/unter Box |
| 7 | Late-Entry | Breakout-Distanz ≤ 2× Box-Range |
| 8 | Candle-Close | Breakout-Kerze muss **geschlossen** sein (5-Min Alignment) |
| 9 | Body-Stärke | Kerzenkörper ≥ 30% der Gesamtlänge |
| 10 | EMA-200 Alignment | 15m-Preis auf Trend-Seite der EMA-200 (H-006) |
| 11 | H4-EMA-50 Alignment | 4H-Trend bestätigt die Trade-Richtung (H-006) |

### Exit-Mechanismus

```
Entry (Market Order mit Preset-SL)
  │
  ├── Stop-Loss        → Box-Boundary ± Buffer (exchange-side)
  ├── TP1 (1:1-R)      → 50% der Position, statisch (exchange-side profit_plan)
  └── TP2 (3:1-R)      → restliche 50%, statisch (exchange-side profit_plan)

Nach TP1-Hit:
  └── Break-Even SL    → SL wird auf Entry + Fee-Buffer verschoben (position_monitor, alle 5 Min)
```

Kein Trailing-Stop — Bitgets nativer Trailing hat bei kleinem Kapital einen 1%-Mindest-Floor,
der bei Andres Setup den Stop auf Höhe des Entries clampt (= wertlos). Statisches TP2 @ 3R ist mathematisch überlegen.

### Sizing

```
Risk-USD   = Balance × 2%
SL-Distanz = |Entry - Stop-Loss|
Size       = Risk-USD / SL-Distanz
```

Hebel wird nur genutzt um Bitgets Mindestordergrößen zu erreichen — nicht um das Risiko zu erhöhen.
Der Dollar-Verlust bei SL-Hit bleibt konstant bei ~2% der Balance.

---

## Sessions & Zeiten (Europe/Berlin)

| Session | Pre-Market | Box | Trade-Scans | Summary |
|---------|-----------|-----|------------|---------|
| Tokyo | 02:00 | 02:15 | 02:00–03:00 (*/5 Min) | 03:30 |
| EU | 08:30 | 09:15 | 09:00–10:00 (*/5 Min) | 10:30 |
| US | 21:00 | 21:15 | 21:00–22:00 (*/5 Min) | 22:55 |

Nightly Report: 01:30 | Daily Closeout: 23:00 | Position Monitor: tägl. */5 Min

---

## Daten die beim Entry geloggt werden

Jeder Trade speichert vollständige Diagnostik in `data/trades.json`:

- **Entry:** Asset, Richtung, Entry-Preis, Size, Hebel, Session
- **Box:** High, Low, Range, Alter, Breakout-Distanz
- **Kerze:** Body-Ratio, Close-Position, Volume-Ratio (vs. 20er-Avg)
- **Trend-Kontext:** EMA-200 (15m), EMA-50 (4H), ATR-14, Alignment-Flags
- **Market-Structure:** Open Interest + OI-Delta (% vs. 5min vorher), Long-Account-%, Taker-Buy-Ratio, Funding Rate
- **Fear & Greed Index:** Tageswert (alternative.me)
- **Slippage:** Fill-Preis vs. Breakout-Trigger in USD

---

## Setup (Server)

```bash
git clone https://github.com/AS90555/apex-trading-bot.git
cd apex-trading-bot
chmod +x setup_server.sh && ./setup_server.sh

# Timezone prüfen (muss Europe/Berlin sein)
timedatectl

# API-Keys eintragen
nano config/.env.bitget    # BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE
nano .env.telegram         # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

# Testen (DRY_RUN=True in bot_config.py lassen bis Telegram-Nachrichten stimmen)
source venv/bin/activate
python scripts/pre_market.py eu
python scripts/autonomous_trade.py
```

---

## Konfiguration (`config/bot_config.py`)

| Parameter | Wert | Beschreibung |
|-----------|------|--------------|
| `DRY_RUN` | `False` | `True` = kein echtes Trading |
| `CAPITAL` | `50.0` | Startkapital in USDT (für Risk-Berechnung) |
| `MAX_RISK_PCT` | `0.02` | 2% Risiko pro Trade |
| `LEVERAGE` | `5` | 5x Hebel |
| `DRAWDOWN_KILL_PCT` | `0.50` | Kill-Switch bei 50% Drawdown |
| `MAX_BOX_AGE_MIN` | `120` | Box maximal 2h alt |
| `MAX_BREAKOUT_DISTANCE_RATIO` | `2.0` | Max Late-Entry (×Box-Range) |
| `H006_EMA_FILTER_ENABLED` | `True` | 15m EMA-200 Alignment-Filter |
| `H006_REQUIRE_H4_ALIGN` | `True` | Zusätzlich 4H EMA-50 Alignment |

---

## Architektur

```
apex-trading-bot/
├── config/
│   ├── bot_config.py          ← Zentrale Konfiguration (alle Parameter hier)
│   └── .env.bitget            ← API-Keys (gitignored, manuell anlegen)
├── scripts/
│   ├── bitget_client.py       ← Exchange-Client (HMAC-Auth, Orders, Market-Data)
│   ├── autonomous_trade.py    ← Breakout-Erkennung, Filter-Gauntlet, Trade-Ausführung
│   ├── position_monitor.py    ← Break-Even, Exit-Logging (alle 5 Min via Cron)
│   ├── save_opening_range.py  ← 15m-Kerze als ORB-Box speichern
│   ├── pre_market.py          ← Session-Start Health Check (Balance, API, offene Positionen)
│   ├── session_summary.py     ← Session-Ende Zusammenfassung via Telegram
│   ├── nightly_report.py      ← Nacht-Report (01:30) via Telegram
│   ├── daily_closeout.py      ← Tages-Abschluss + Anomalie-Check via Telegram
│   ├── weekend_momo.py        ← Weekend-Momentum-Strategie (AVAX, Sa–So)
│   ├── apex_status.py         ← Einbefehl-Statuscheck (Balance, Trades, P&L)
│   └── telegram_sender.py     ← Telegram-Wrapper
├── data/                      ← Laufzeit-Daten (gitignored)
│   ├── trades.json            ← Alle Trades mit vollständiger Diagnostik
│   ├── opening_range_boxes.json
│   ├── pnl_tracker.json       ← P&L-Tracker mit Win/Loss-Counter
│   ├── skip_log.jsonl         ← Jeder Skip mit Grund (Analyse Skip-Funnel)
│   ├── pending_notes.jsonl    ← Exit-Notes für Claude-Session-Verarbeitung
│   └── high_water_mark.json
├── logs/                      ← Log-Dateien (gitignored, Rotation bei >1MB)
│   ├── tokyo.log / eu.log / us.log
│   ├── monitor.log
│   └── daily.log
├── .env.telegram              ← Telegram-Keys (gitignored)
└── setup_server.sh            ← Einmaliges Server-Setup
```

---

## Sicherheitsmechanismen

| Mechanismus | Beschreibung |
|-------------|--------------|
| **Kill-Switch** | Keine neuen Trades wenn Balance < 50% Startkapital |
| **Preset SL** | Market Order enthält SL als Preset → SL ist vom ersten Moment an aktiv |
| **SL-Validierung** | Nach Fill wird geprüft ob Preset-SL aktiv ist; sonst erneuter Versuch, sonst Notschließung |
| **Emergency-Close** | Position sofort schließen wenn kein SL/TP gesetzt werden konnte |
| **Lock-File** | Verhindert parallele Cron-Ausführung (`data/autonomous_trade.lock`) |
| **Orphan-Cleanup** | Verbleibende TP/SL-Orders vor Trade bereinigen |
| **Isolated Margin** | Verlust begrenzt auf hinterlegte Margin |
| **Fail-Safe EMA** | Fehlende EMA-Daten blockieren nicht — Trade läuft durch (kein False-Negative) |
| **Atomares Schreiben** | Alle JSON-Writes via tmp+rename (keine Korruption bei Absturz) |

---

## Bot steuern

```bash
# Status prüfen
source venv/bin/activate && python scripts/apex_status.py

# Logs live verfolgen
tail -f logs/eu.log
tail -f logs/monitor.log

# Bot pausieren
crontab -l > ~/apex_cron_backup.txt && crontab -r

# Bot wieder starten
crontab ~/apex_cron_backup.txt

# Laufendes Script abbrechen
pkill -f autonomous_trade.py
```

---

## Bitget API

- **Endpoint:** `https://api.bitget.com`
- **Product Type:** `USDT-FUTURES`
- **Auth:** HMAC-SHA256 (API Key + Secret + Passphrase)
- **Rechte:** Lesen + Futures-Trading (kein Auszahlen)
- **Docs:** https://www.bitget.com/api-doc/contract/intro
