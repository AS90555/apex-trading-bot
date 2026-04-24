# Trade Log — Micro-Analysen

---

### 2026-04-21 04:10 | ETH | TOKYO | -1.02R | LOSS
Entry Long $2318.55, SL getriggert bei $2306.64. Vol=3.7x (stark), Slip=$1.41 (hoch für ETH — typisch Tokyo thin liquidity). Box-Age 35min, normale Setup-Qualität. Kein Filter hätte dies verhindert außer dem ETH-Blacklist-Kandidaten-Status.

---

### 2026-04-21 14:40 | ETH | EU | +0.04R | PARTIAL_WIN
Fast Break-Even. Entry Long $2320.80, exit $2306.63 — PARTIAL_WIN bedeutet TP1 leicht getroffen aber kein echter Gewinner. Vol=2.6x, Slip=$1.56. ETH zeigt wieder hohes Slippage (~$1.50 konsistent). Marginaler Gewinn der in der Praxis durch Fees aufgefressen wird.

---

### 2026-04-21 21:40 | ETH | US | -1.22R | LOSS
Voller Verlust. Vol=2.4x (knapp über Threshold), Body=0.803 (zu nah an 0.8 Grenze — schwaches Setup). Box-Age nur 10min — sehr frische Box, wenig validiert. Slip=$1.77. Drei ETH-Trades in einem Tag = Konzentrations-Problem.

---

### 2026-04-22 05:15 | SOL | TOKYO | +2.06R | BE_TP1_WIN ✅
Bester Trade der Woche. BE angewendet, TP1 getroffen. Vol=3.9x (sehr stark), Slip=$0.10 (minimal — SOL-typisch). Box-Age nur 5min aber sofort bestätigt. SOL verhält sich wie erwartet aus VAA-Validierung: sauber, niedriges Slippage, gutes Follow-Through.

---

### 2026-04-22 10:35 | SOL | EU | -0.99R | LOSS
Vol=1.039x — knapp über 1.0, weit unter normalem Threshold. **Schwaches Setup** — warum wurde dieser Trade genommen? Vol-Filter sollte ≥2.0x verlangen für ORB. Box-Age 45min (alt). Slip=$0.005 (korrekt für SOL). Loss ist akzeptabel aber Setup-Qualität war ungenügend.

---

### 2026-04-23 01:25 | ETH | US | -1.09R | LOSS
Vierter ETH-Verlust in Serie. Vol=1.297x — wieder unter normalem Schwellenwert. Body=0.826 — über 0.8, eigentlich Grenzfall. Box-Age 5min. Slip=$1.55 (ETH-Slippage-Problem bleibt konstant). ETH häuft Verluste an, Slippage frisst Edge komplett auf.

---

### 2026-04-23 17:20 | SOL | EU | +0.10R | BE_PARTIAL_WIN
Short-Trade, BE angewendet. Entry $85.852, Exit $86.227 — BE getriggert kurz bevor Preis weiter lief. Vol=1.497x (deutlich unter 2.5x VAA-Threshold, für ORB aber akzeptabel). Body=0.877 (kein starkes Setup). Box-Age 40min, Breakout-Distanz $0.193 bei Box-Range $0.151 = 128% — knapp über MAX_BREAKOUT_DISTANCE_RATIO-Grenze. Slip=$0.077 (SOL-typisch niedrig). Positives Ergebnis aber schwaches Setup — BE hat hier das Ergebnis gerettet.

---
