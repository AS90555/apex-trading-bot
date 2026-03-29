# APEX Trading Bot – Claude Code Kontext

## Wer bin ich / Wer ist der User

- **User:** Andre (nicht Christian – das ist der Kollege dessen Repo als Basis diente)
- **Ziel:** Den Bot für Andres eigenes Setup adaptieren und produktiv auf einem Server laufen lassen
- **Sprache:** Deutsch bevorzugt
- **Stil:** Direkt, kein Overhead, kurze Antworten

---

## Was ist APEX

Vollautomatischer Krypto-Trading-Bot mit zwei Strategien:
1. **ORB (Opening Range Breakout)** – Hauptstrategie, Mo–Fr, 3 Sessions/Tag
2. **WeekendMomo** – Wochenend-Momentum für AVAX

Keine KI für Trading-Entscheidungen – rein algorithmische Technische Analyse.

---

## Andres Setup (abweichend vom Kollegen)

| Parameter | Kollege (Original) | Andre |
|---|---|---|
| Exchange | Hyperliquid (DEX) | **Bitget (CEX)** |
| Kapital | ~$2.300 | **50 USDT** |
| Orchestrierung | OpenClaw + 26 Cron-Jobs | **Linux crontab** |
| Auto-Deploy | n8n | **entfällt** |
| Assets | BTC, ETH, SOL, AVAX | **ETH, SOL, AVAX** (BTC zu groß bei 50 USDT) |
| Hebel | variabel | **5x** |
| KI | Gemini via OpenClaw | **nicht nötig** |
| Modus | Live | **DRY_RUN=True** (zum Start) |

**Wichtig:** 2% Risiko pro Trade = $1 bei 50 USDT. Hebel nur um Mindestordergrößen zu erreichen, nicht zum Risiko-Amplify.

---

## Architektur nach Migration

```
apex-trading-bot/
├── CLAUDE.md                  ← Diese Datei
├── config/
│   ├── bot_config.py          ← ZENTRALE KONFIGURATION (DRY_RUN, CAPITAL, LEVERAGE etc.)
│   └── .env.bitget            ← API-Keys (gitignored, muss manuell erstellt werden)
├── scripts/
│   ├── bitget_client.py       ← Haupt-Exchange-Client (ersetzt hyperliquid_client.py)
│   ├── autonomous_trade.py    ← Breakout-Erkennung & Order-Ausführung (Cron-Trigger)
│   ├── weekend_momo.py        ← Weekend-Momentum-Strategie
│   ├── save_opening_range.py  ← Opening Range Box speichern
│   ├── position_monitor.py    ← Offene Positionen überwachen
│   ├── pre_market.py          ← Session-Start Health Check
│   ├── daily_closeout.py      ← Tages-Abschluss Report
│   ├── session_summary.py     ← Session-Ende Summary
│   └── telegram_sender.py     ← Telegram Notifications (unverändert)
├── data/                      ← Laufzeit-Daten (gitignored)
│   ├── opening_range_boxes.json
│   ├── trades.json
│   └── ...
├── logs/                      ← Log-Dateien (gitignored)
└── setup_server.sh            ← Einmaliges Server-Setup-Skript
```

---

## Was bereits vollständig erledigt ist

- [x] `bitget_client.py` geschrieben (HMAC-Auth, Market/SL/TP Orders, DRY_RUN)
- [x] `config/bot_config.py` mit allen Parametern
- [x] Alle 7 Skripte auf Bitget umgestellt
- [x] OpenClaw-Pfade (`/data/.openclaw/...`) entfernt → relative Pfade
- [x] `setup_server.sh` geschrieben (Python venv, Dependencies, Crontab)
- [x] `.gitignore` aktualisiert (`.env.bitget`, `.claude/`, `data/` geschützt)
- [x] Lokal getestet: Marktdaten, Candles, Orderbook ✅
- [x] GitHub Repo: https://github.com/AS90555/apex-trading-bot

---

## Was auf dem Server noch zu tun ist

1. **Repo klonen**
   ```bash
   git clone https://github.com/AS90555/apex-trading-bot.git
   cd apex-trading-bot
   ```

2. **Setup ausführen**
   ```bash
   chmod +x setup_server.sh && ./setup_server.sh
   ```

3. **Timezone prüfen** (muss Europe/Berlin sein!)
   ```bash
   timedatectl
   sudo timedatectl set-timezone Europe/Berlin
   ```

4. **API-Keys eintragen**
   ```bash
   nano config/.env.bitget
   # BITGET_API_KEY=...
   # BITGET_SECRET_KEY=...
   # BITGET_PASSPHRASE=...

   nano .env.telegram
   # TELEGRAM_BOT_TOKEN=...
   # TELEGRAM_CHAT_ID=...
   ```

5. **Testen mit echten Credentials**
   ```bash
   source venv/bin/activate
   python scripts/bitget_client.py     # Marktdaten + Balance
   python scripts/pre_market.py eu     # Vollständiger Check
   ```

6. **Live schalten**
   ```bash
   # In config/bot_config.py:
   # DRY_RUN = False
   ```

---

## Server-Details

- **OS:** Ubuntu 24.04.4 LTS (GNU/Linux 6.8.0-90 generic aarch64)
- **User:** root
- **Python:** system python3 + venv unter `./venv/`
- **Crontab:** Eingerichtet via `setup_server.sh`
- **Logs:** `./logs/*.log` (rotation bei >5MB)

---

## Bitget API

- **Dokumentation:** https://www.bitget.com/api-doc/contract/intro
- **Endpoint:** `https://api.bitget.com`
- **Product Type:** `USDT-FUTURES`
- **Auth:** HMAC-SHA256 (API Key + Secret + Passphrase)
- **Benötigte Rechte:** Lesen + Futures-Trading (kein Auszahlen nötig)

---

## Wichtige Konfigurationsparameter (`config/bot_config.py`)

```python
DRY_RUN = True        # ← AUF FALSE SETZEN FÜR LIVE
CAPITAL = 50.0        # USDT
LEVERAGE = 5          # 5x Hebel
MAX_RISK_PCT = 0.02   # 2% = $1 pro Trade
ASSETS = ["ETH", "SOL", "AVAX"]
```

---

## Kritische Hinweise

- **BTC nicht handelbar** bei 50 USDT (Mindestorder > max. Positionsgröße)
- **Telegram** benötigt `.env.telegram` im Projektroot (nicht in `config/`)
- **DRY_RUN immer zuerst** – erst wenn Telegram-Nachrichten korrekt ankommen live schalten
- **Timezone muss Berlin sein** – alle Cron-Zeiten sind Europe/Berlin
- Die Datei `config/.env.bitget` muss manuell auf dem Server erstellt werden (nie in Git!)

---

## Häufige Befehle auf dem Server

```bash
# Logs live beobachten
tail -f logs/eu.log
tail -f logs/us.log

# Bot manuell testen
source venv/bin/activate
python scripts/pre_market.py eu
python scripts/save_opening_range.py
python scripts/autonomous_trade.py

# Crontab anzeigen
crontab -l

# Weekend Momo Status
python scripts/weekend_momo.py --status

# Bot-Status (Balance + Positionen)
python -c "from scripts.bitget_client import BitgetClient; c = BitgetClient(dry_run=False); print(c.format_status())"
```
