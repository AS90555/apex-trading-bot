# APEX Trading Bot

Vollautomatischer Krypto-Trading-Bot mit zwei Strategien:
- **ORB (Opening Range Breakout)** – Mo–Fr, 3 Sessions/Tag (Tokyo, EU, US)
- **WeekendMomo** – Wochenend-Momentum auf AVAX

**Exchange:** Bitget (USDT-Perpetual Futures)
**Kapital:** 50 USDT | **Hebel:** 5x | **Risk/Trade:** 2% (~$1)
**Assets:** ETH, SOL, AVAX (kein BTC – Mindestorder zu groß bei 50 USDT)

---

## Setup (Server)

```bash
git clone https://github.com/AS90555/apex-trading-bot.git
cd apex-trading-bot
chmod +x setup_server.sh && ./setup_server.sh

# Timezone prüfen (muss Europe/Berlin sein)
timedatectl

# API-Keys eintragen
nano config/.env.bitget        # BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE
nano .env.telegram             # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

# Testen
source venv/bin/activate
python scripts/bitget_client.py
python scripts/pre_market.py eu
```

---

## Konfiguration (`config/bot_config.py`)

| Parameter | Wert | Beschreibung |
|---|---|---|
| `DRY_RUN` | `True` | Auf `False` für Live-Trading |
| `CAPITAL` | `50.0` | Startkapital in USDT |
| `MAX_RISK_PCT` | `0.02` | 2% Risiko pro Trade |
| `LEVERAGE` | `5` | 5x Hebel |
| `MARGIN_MODE` | `isolated` | Isolated (sicherer) oder crossed |
| `DRAWDOWN_KILL_PCT` | `0.50` | Kill-Switch bei 50% Drawdown |

---

## Cron-Schedule (Europe/Berlin)

| Zeit | Script | Beschreibung |
|---|---|---|
| Mo–Fr 02:00 | `pre_market.py tokyo` | Tokyo Pre-Market Check |
| Mo–Fr 02:15 | `save_opening_range.py` | Box speichern |
| Mo–Fr 02:30–03:00 | `autonomous_trade.py` (3x) | Breakout-Checks |
| Mo–Fr 03:30 | `session_summary.py tokyo` | Session-Zusammenfassung |
| Mo–Fr 08:30 | `pre_market.py eu` | EU Pre-Market Check |
| Mo–Fr 09:00 | `save_opening_range.py` | Box speichern |
| Mo–Fr 09:15–10:00 | `autonomous_trade.py` (3x) | Breakout-Checks |
| Mo–Fr 10:30 | `session_summary.py eu` | Session-Zusammenfassung |
| Mo–Fr 21:00 | `pre_market.py us` | US Pre-Market Check |
| Mo–Fr 21:30 | `save_opening_range.py` | Box speichern |
| Mo–Fr 21:45–22:15 | `autonomous_trade.py` (4x) | Breakout-Checks |
| Mo–Fr 23:00 | `daily_closeout.py` | Tages-Abschluss |
| tägl. */30 | `position_monitor.py` | Offene Positionen |
| Fr 23:00 | `weekend_momo.py --check` | WeekendMomo Signal-Check |
| Sa 00:05 UTC | `weekend_momo.py --entry` | WeekendMomo Entry |
| So 21:00 | `weekend_momo.py --exit` | WeekendMomo Exit |

---

## Bot steuern

```bash
# Pausieren
crontab -l > ~/apex_cron_backup.txt && crontab -r

# Wieder starten
crontab ~/apex_cron_backup.txt

# Aktiv? (0 = pausiert, >0 = läuft)
crontab -l | grep -c scripts

# Laufendes Script abbrechen
pkill -f autonomous_trade.py

# Logs live
tail -f logs/eu.log
tail -f logs/us.log
tail -f logs/monitor.log
```

---

## Architektur

```
apex-trading-bot/
├── config/
│   ├── bot_config.py          ← Zentrale Konfiguration
│   └── .env.bitget            ← API-Keys (gitignored)
├── scripts/
│   ├── bitget_client.py       ← Exchange-Client (HMAC-Auth, Orders, Balance)
│   ├── autonomous_trade.py    ← Breakout-Erkennung & Trade-Ausführung
│   ├── weekend_momo.py        ← Weekend-Momentum-Strategie (AVAX)
│   ├── save_opening_range.py  ← Opening Range Box speichern
│   ├── position_monitor.py    ← Offene Positionen überwachen
│   ├── pre_market.py          ← Session-Start Health Check
│   ├── daily_closeout.py      ← Tages-Abschluss Report
│   ├── session_summary.py     ← Session-Ende Summary
│   └── telegram_sender.py     ← Telegram Notifications
├── data/                      ← Laufzeit-Daten (gitignored)
├── logs/                      ← Log-Dateien (gitignored)
├── .env.telegram              ← Telegram-Keys (gitignored)
├── setup_server.sh            ← Einmaliges Server-Setup
└── ERKENNTNISSE.md            ← Lessons Learned & bekannte Issues
```

---

## Sicherheitsmechanismen

- **Kill-Switch:** Keine neuen Trades wenn Balance < 50% Startkapital
- **Preset SL/TP:** Jede Market Order enthält SL/TP als Preset
- **Preset-Check:** Nach Fill prüfen ob Preset-SL/TP aktiv, nur dann separat setzen wenn nicht
- **Emergency-Close:** Position sofort schließen wenn wirklich kein SL/TP gesetzt werden konnte
- **Lock-File:** Verhindert parallele Ausführung durch Cron-Überlappung
- **Orphan-Cleanup:** Verbleibende TP/SL-Orders vor jedem Trade löschen
- **Isolated Margin:** Verlust begrenzt auf hinterlegte Margin (nicht gesamte Balance)

---

## Bitget API

- **Endpoint:** `https://api.bitget.com`
- **Product Type:** `USDT-FUTURES`
- **Auth:** HMAC-SHA256 (API Key + Secret + Passphrase)
- **Rechte:** Lesen + Futures-Trading (kein Auszahlen)
- **Docs:** https://www.bitget.com/api-doc/contract/intro
