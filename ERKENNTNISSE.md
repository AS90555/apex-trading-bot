# APEX - Erkenntnisse & Learnings

*Letzte Aktualisierung: 2026-03-30*

---

## 🔧 Technische Erkenntnisse (Bitget-spezifisch)

### SL/TP Doppel-Placement Konflikt ⚠️ KRITISCH (behoben)

**Problem:** Die Market Order schickt bereits `presetStopLossPrice` und `presetStopSurplusPrice`
mit. Wenn Bitget diese als Preset akzeptiert, lehnt der nachfolgende separate
`place-tpsl-order` Call mit Fehler ab (Konflikt). Der Code interpretierte das
als "keine SL/TP gesetzt" und löste Emergency-Close aus – obwohl die Position
bereits durch das Preset geschützt war.

**Symptom im Log:** `SL: ❌ | TP: ❌` obwohl Trade eigentlich geschützt wäre.
Position bleibt ungeschützt wenn Emergency-Close auch fehlschlägt.

**Fix:** Nach dem Fill erst `get_tpsl_orders()` aufrufen (GET `/api/v2/mix/order/tpsl-pending`).
Wenn Preset-SL/TP aktiv (`loss_plan` / `profit_plan`) → separat nichts mehr setzen.
Nur wenn wirklich keine TPSL-Orders existieren → separat setzen + Emergency-Close als letzter Fallback.

**Commit:** `dbed1c4`

---

### cancel-plan-order braucht orderId ⚠️ (behoben)

**Problem:** `POST /api/v2/mix/order/cancel-plan-order` erfordert eine `orderId`.
Der Code schickte den Request ohne `orderId` → API-Fehler, Orphan Orders
wurden nicht bereinigt.

**Fix:** Endpoint gewechselt auf `POST /api/v2/mix/order/cancel-all-plan-order`
(cancelt alle Plan-Orders eines Symbols ohne orderId).

**Commit:** `c449edc`

---

### Bitget avg_price ist Näherung

**Verhalten:** `place_market_order()` gibt als `avg_price` den aktuellen Marktpreis
*nach* dem Fill zurück (via `get_price()`), nicht den tatsächlichen Fill-Preis
aus der API-Response. Bei Market Orders auf liquiden Paaren (ETH/SOL/AVAX)
ist die Abweichung typischerweise < 0.05%, für unsere SL/TP-Distanzen vernachlässigbar.

---

### Sleep-Zeit nach Market Order

**Erfahrung:** 3 Sekunden waren manchmal nicht ausreichend damit Bitget die Position
in `get_positions()` anzeigt. Auf 5 Sekunden erhöht.

---

### Isolated vs. Cross Margin

**Entscheidung:** `MARGIN_MODE = "isolated"` ist für kleine Konten (<$100) klar besser.
Bei Cross Margin kann eine einzige liquidierte Position die gesamte Balance vernichten.
Isolated begrenzt den maximalen Verlust pro Trade auf die hinterlegte Margin.

**Commit:** `c2f63a6`

---

## 📊 Erste Live-Trades (2026-03-30)

### Trade 1: ETH LONG (Tokyo Session) ✅ WIN
- Entry: $2,007.00 | Size: 0.05 ETH
- Stop-Loss: $1,977.75 | Take-Profit: $2,065.50
- Risk: $1.37 | Reward: $2.73 (2:1)
- Ergebnis: **TP getroffen**, Balance $68.33 → $71.11 (+$2.78)
- SL/TP: Preset + Separate jeweils ✅ (kein Konflikt)

### Trade 2: SOL LONG (EU Session) ❌ MANUELL GESCHLOSSEN
- Entry: $83.97 | Size: 1.6 SOL
- Stop-Loss: $83.115 | Take-Profit: $85.68
- Risk: $1.42 | Reward: $2.84 (2:1)
- Ergebnis: SL ❌ TP ❌ im Log → Position ohne Schutz offen
- Ursache: Alter Code (vor Fix `dbed1c4`) + Preset-Konflikt
- Manuell auf Bitget geschlossen, kein größerer Verlust

**Lektion:** Bot niemals LIVE lassen bevor alle Fixes deployed und getestet sind.

---

## 📋 Checkliste vor jeder neuen Session

```
[ ] crontab -l | grep -c scripts  → muss > 0 sein
[ ] python scripts/pre_market.py eu  → API + Balance + Telegram OK?
[ ] Keine offenen Positionen ohne SL/TP auf Bitget UI prüfen
[ ] logs/monitor.log auf Fehler scannen: grep -i "fehler\|error\|kritisch" logs/*.log
[ ] Balance > Kill-Switch-Schwelle ($25 bei 50 USDT Start)?
[ ] Timezone korrekt: timedatectl | grep Berlin
```

---

## ⚠️ Bekannte Schwachstellen / Todos

| Priorität | Thema | Beschreibung |
|---|---|---|
| MITTEL | Spread-Check | `MAX_SPREAD_PCT` konfiguriert aber nie geprüft. Bei illiquiden Zeiten (Tokyo AVAX) können Spreads >0.5% sein → schlechte Fills. |
| MITTEL | Tokyo Session | AVAX bei ~$8.85 = 1 AVAX Mindestgröße ≈ gesamter Risk-Betrag. Tokyo generell illiquid für diese Assets. Evaluieren ob Tokyo-Crons sinnvoll. |
| NIEDRIG | Fill-Preis | `avg_price` aus `get_price()` nach Fill, nicht echter Fill-Preis. Für SL/TP-Berechnung marginale Abweichung. |
| NIEDRIG | Session-Zeiten | US Session: 21:00–23:00 Berlin. Bei Sommerzeit vs. Winterzeit aufpassen – Server läuft auf Europe/Berlin, passt sich automatisch an. |

---

## 💡 Strategie-Beobachtungen

### ORB-Qualität hängt von der Box-Range ab
- Opening Range 0.00 (z.B. ETH High = Low = $1,982.75) → keine echte Range
- Das passiert wenn `save_opening_range.py` nur 1 Candle erwischt oder der Markt flach ist
- Breakout-Threshold verhindert dann theoretisch schlechte Entries
- **Todo:** Box-Range-Minimum einbauen (z.B. mind. 0.1% Range nötig)

### Fees bei 50 USDT Konto
- Bitget Taker Fee: 0.1% | Maker Fee: 0.06%
- Beispiel SOL-Trade: $83.97 × 1.6 = $134 Notional → $0.13 Gebühren Round-Trip
- Das sind ~9% des $1.42 Risk-Betrags
- Break-Even Win-Rate bei 2:1 RR und 9% Fees: ~37% (gut)
- Aber bei kleineren Positionen (AVAX) → Gebühren-Anteil steigt

---

## 🖥 Server-Info

- **IP:** 178.104.58.214 | **User:** root
- **OS:** Ubuntu 24.04.4 LTS (aarch64)
- **Python:** venv unter `~/apex-trading-bot/venv/`
- **Timezone:** Europe/Berlin (CEST, +0200) ✅
- **Crontab:** via `setup_server.sh` eingerichtet
- **Logs:** `~/apex-trading-bot/logs/*.log`

### Wichtige Server-Befehle

```bash
# Bot pausieren / starten
crontab -l > ~/apex_cron_backup.txt && crontab -r    # pausieren
crontab ~/apex_cron_backup.txt                         # starten

# Status prüfen
source venv/bin/activate
python3 scripts/pre_market.py eu

# Logs
tail -f logs/us.log
grep -i "kritisch\|error\|fehler" logs/*.log

# Updates einspielen (Server hat lokale bot_config.py Änderungen → stash nötig)
git stash && git pull origin main && git stash pop
```

### Bekanntes Server-Verhalten
- `config/bot_config.py` hat lokale Änderungen (DRY_RUN=False, evtl. andere Werte)
- `git pull` schlägt ohne `git stash` fehl → immer `git stash && git pull && git stash pop`
- Terminal friert manchmal nach langen Log-Outputs ein → neue SSH-Session öffnen
- `python` nicht im PATH außerhalb venv → `python3` oder `source venv/bin/activate` zuerst
