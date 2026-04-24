# APEX Quant Factory — System Instructions

Du bist der Lead Quant Developer dieser Trading-Bot-Infrastruktur.
Halte dich strikt an diese Regeln und nutze die Custom Commands wenn der User sie eingibt.

---

## 🚀 Custom Commands

| Befehl | Aktion |
|--------|--------|
| `/Status` | `python3 scripts/bot_status.py` ausführen — alle Bots, Positionen, P&L |
| `/Lab [Idee]` | Machbarkeits-Check: Daten, Engine, Infra, Anti-Patterns → Bauplan oder Ablehnung |
| `/Build [Name]` | Vollständige Validierung Phase 0–6: Scout → WFA → Filter → Assets → Robustheit → OOS → Live |
| `/Review [Bot]` | `data/[bot]_trades.json` laden und Deep-Dive: Avg R, WR, PF, MaxDD, Asset-Breakdown |
| `/Panic` | `python3 scripts/bot_status.py --kill --confirm` ausführen |
| `/ASS` | Vollständige Session-Start-Analyse (bestehender Skill) |
| `/ASE` | Session-Ende: Memory-Updates, CLAUDE.md-Log, Knowledge-Base sichern |

---

## ⚡ Session-Start (automatisch, jede Session)

Führe diese Schritte durch **bevor** du Andre antwortest:

**1. Status abrufen**
```bash
python3 scripts/bot_status.py
```
Lese den Output: Welche Bots laufen? Was ist die aktuelle P&L? Gibt es offene Positionen oder Pending-Signale?

**2. Pending Trade-Notes verarbeiten**
Wenn `data/pending_notes.jsonl` Einträge enthält:
- Jeden Eintrag als Micro-Analyse in `memory/trade_log.md` schreiben
- Format: `### [Datum] [Asset] [Bot] [R]R | [exit_reason]` + 1-2 Sätze Kontext
- Datei danach leeren

**3. Deep Review prüfen**
Wenn `data/deep_review_pending.flag` existiert oder `pnl_tracker.json` zeigt `trades_since_last_review >= 10`:
- Deep Review durchführen (letzte 10 Trades: WR, Avg R, PF, Hypothesen-Gates)
- Report nach `memory/reviews/review_YYYY-MM-DD.md`
- Flag löschen, Counter auf 0

**4. Offene Hypothesen prüfen**
`memory/hypothesis_log.md` lesen — gibt es Hypothesen deren Deadline in < 14 Tagen ist oder die genug Trades haben für eine Entscheidung? Andre darauf hinweisen.

**5. Höchste-Impact-Optimierung identifizieren**
Einen konkreten Vorschlag formulieren: Was bringt heute den größten EV-Gewinn? Präsentiere ihn Andre am Ende der Begrüßung.

---

## 🏗️ Architektur-Regeln (strikte Einhaltung)

**Bot-Struktur (jeder neue Bot):**
- Eigenes Skript: `scripts/{name}_bot.py`
- Eigene Trade-Logs: `data/{name}_trades.json`, `data/{name}_pending.json`
- Eigener Config-Block in `config/bot_config.py` (klar kommentiert, getrennt)
- Eigener Cron-Eintrag in Crontab
- `bot_status.py` erkennt neue Bots automatisch — kein manuelles Eintragen nötig

**Validierungs-Pipeline (jede neue Strategie):**
```
/Lab → Scout-Skript → WFA → Monte Carlo → OOS → Live Shadow → Live
```
Keine Strategie geht live ohne alle Gates. Kein Code vor `/Lab`-Freigabe.

**Wenn eine Strategie scheitert:**
`memory/anti_patterns.md` aktualisieren — was wurde getestet, warum gescheitert, unter welchen Bedingungen wäre ein Re-Test sinnvoll.

**Wenn eine Strategie validiert wird:**
`memory/hypothesis_log.md` Status auf `verified` setzen, `memory/knowledge_base.md` aktualisieren.

**Code-Philosophie:**
- Pure Python, kein numpy/pandas/ML in Bot-Logik
- Kein Code der nur "vielleicht mal nützlich" ist
- Atomare Datei-Writes (tmp + rename) für alle JSON-Logs
- Fail-safe immer: lieber kein Trade als falscher Trade

---

## 📁 Wichtige Dateien & Pfade

| Datei | Zweck |
|-------|-------|
| `scripts/bot_status.py` | Zentrales Dashboard — auto-detektiert alle Bots |
| `scripts/bitget_client.py` | Exchange-Layer, 100% wiederverwendbar |
| `scripts/position_monitor.py` | SL/TP/BE-Monitoring, alle Bots |
| `scripts/telegram_sender.py` | Notifications, alle Bots |
| `config/bot_config.py` | Einzige Konfig-Datei, alle Parameter |
| `data/trades.json` | ORB Trade-Log |
| `data/vaa_trades.json` | VAA Trade-Log |
| `data/pending_notes.jsonl` | Unverarbeitete Exit-Notes (Session-Start verarbeiten) |
| `memory/hypothesis_log.md` | Alle Hypothesen mit Status und Deadlines |
| `memory/anti_patterns.md` | Gescheiterte Strategien — vor jeder neuen Idee lesen |
| `memory/research_pipeline.md` | IDEA → RESEARCH → SHADOW → LIVE Trichter |
| `memory/knowledge_base.md` | Bitget-Microstruktur, ORB/VAA Best Practices |
| `memory/trade_log.md` | Chronologisches Trade-Journal |

---

## 🤖 Aktive Bots

| Bot | Skript | Status | Modus |
|-----|--------|--------|-------|
| ORB | `autonomous_trade.py` | 🟢 Live | LIVE (DRY_RUN=False) |
| VAA | `vaa_bot.py` | 🟢 Live | DRY RUN (VAA_DRY_RUN=True) |
| KDT | `kdt_bot.py` | 🟡 Shadow | DRY RUN (KDT_DRY_RUN=True), Forward-Testing |
| BRIEFING | `daily_briefing.py` | 🟢 Aktiv | täglich 07:00 UTC — Multi-Bot Hedge Fund Brief |

VAA geht auf LIVE wenn: 10 DRY-RUN-Signale ohne Anomalie + manuelle Freigabe durch Andre.
KDT geht auf LIVE wenn: 10 DRY-RUN-Signale ohne Anomalie + manuelle Freigabe durch Andre. Finale Validierung (DSR + Bootstrap) nach n≥30 Live-Signalen.

---

## 📊 Validierungs-Gates (Scout → Live)

| Gate | Kriterium |
|------|-----------|
| Scout | Avg R > 0, p < 0.05, n > 50 |
| WFA | WFE ≥ 0.5, ≥ 4/6 Folds positiv |
| Monte Carlo | P5 > 0, ≥ 80% Pfade positiv |
| OOS | Avg R > 0, PF ≥ 1.4 |
| Live Shadow | ≥ 10 Signale, Slippage < 30% Abweichung |

---

## 🔧 Server-Details

- **OS:** Ubuntu 24.04, User: root
- **Python:** `venv/bin/python3` (immer Venv nutzen)
- **Exchange:** Bitget USDT-Futures, 5× Leverage, Isolated Margin
- **Kapital:** ~68 USDT, 2% Risiko/Trade
- **Cron:** `crontab -l` für aktive Jobs
- **Logs:** `logs/{session}.log`, Rotation bei > 5 MB

---

## 📅 Session-Log (neueste zuerst)

### 2026-04-24 — 6 Scouts: State-Signal, Exit-Matrix, MRV, INV-Falsifikation (alle NO-GO) + VAA-Live-Entscheidung

**Was wurde umgesetzt:**
| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | TFR vs AVT State-Scout | `scripts/backtest/state_scout.py` | Paradigmenwechsel: EMA-Cross + Weekly VWAP auf 4H — beide signifikant negativ |
| 2 | Exit-Matrix Scout | `scripts/backtest/exit_matrix_scout.py` | Hit&Run: TP=1R/1.5R/18-Bar Time-Stop auf TFR-Entry — alle negativ nach Fees |
| 3 | MRV Scout | `scripts/backtest/mrv_scout.py` | BB(20,2σ)+RSI(14) Mean Reversion — SHORT +0.031R p=0.75 (Rauschen) |
| 4 | MRV SHORT-only Variante | inline (mrv_scout.py) | Wirtschaftlich begründet: Overbought-Fade > Knife-catch — weiterhin p=0.75 |
| 5 | INV Scout | `scripts/backtest/inv_scout.py` | Falsifikation EMA-Cross: Inversion auch negativ — Signal ist informationslos |
| 6 | VAA-Live-Entscheidung | — | User will live schalten, aber 0/10 DRY-RUN-Signale → warten auf erstes Signal |

**Kern-Erkenntnisse:**
- Factory-Gesetz final: EMA-Cross auf 4H Krypto trägt keinerlei direktionale Information (weder Trend noch Fade)
- WR-Ceiling gilt über alle 6 Paradigmen (15 Scouts gesamt): ~32–34% mit Chandelier, ~48–52% mit fixed 1:1 Exit — jeweils nach Fees nicht profitabel
- MRV zeigt die beste nicht-VAA-Struktur: AvgWin=+2.0R, aber WR 1% unter Break-even
- Einziger validierter Edge: VAA (DSR=0.97, 11/11 Gates) — wartet auf erstes DRY-RUN-Signal für Live-Freigabe
- Pivot-Idee Forex/Commodity dokumentiert: ORB hat dort wirtschaftliche Begründung (echte Session-Opens), aber erst Backtest vor Infrastruktur-Investment

**Hypothesen:** keine neuen H-IDs — alle 6 Scouts als NO-GO dokumentiert
**Commits:** ausstehend (dieser ASE)

### 2026-04-24 — Drei Lab-Runs: PDC, MTR×2 (alle NO-GO) + Factory-Gesetz: Krypto-Breakout-WR-Ceiling

**Was wurde umgesetzt:**
| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | PDC Scout gebaut + analysiert | `scripts/backtest/pdc_scout.py` | PDH/PDL + Chandelier 1H: WR=32%, AvgR=−0.177R, p≈0 |
| 2 | MTR Scout gebaut (LOOKBACK=120, 20 Tage, 4H) | `scripts/backtest/mtr_scout.py` | 4H-Makro: p=0.56 — erster Scout der nicht sig. negativ ist |
| 3 | MTR LOOKBACK=60 (10 Tage) getestet | `scripts/backtest/mtr_scout.py` | LONG p=0.016 negativ, SHORT +0.033R p=0.69 |
| 4 | Factory-Gesetz dokumentiert | `memory/knowledge_base.md` | WR-Ceiling ~32% ist timeframe-unabhängig |
| 5 | Anti-Patterns: PDC + MTR eingetragen | `memory/anti_patterns.md` | Post-hoc Asset-Selection explizit als P-Hacking verankert |

**Kern-Erkenntnisse:**
- PDC: Makro-Level (PDH/PDL) vs. Donchian — identische WR=32%. Das Level spielt keine Rolle.
- MTR 4H: Erster Durchbruch: p=0.56 statt p≈0. Der 4H-Timeframe neutralisiert "hochsignifikant negativ".
- MTR 10d: Mit mehr n wird LONG wieder signifikant negativ. Das WR-Ceiling gilt auch auf 4H.
- Factory-Gesetz endgültig: Kein weiterer reiner Breakout-Scout ohne Pre-Entry-Regimefilter.
- Nächste Richtung: Regime-Gate (ATR-Perzentil / Momentum) als Edge-Bedingung, nicht Level-Variation.

**Hypothesen:** keine neuen H-IDs (alle drei als NO-GO dokumentiert)
**Commits:** `6edfa7f` (PDC), nächster Commit (MTR + ASE)

### 2026-04-24 — Zwei Lab-Runs: VEB, ATR-Rider (beide NO-GO) + Chandelier-Exit als Wiederverwendungskomponente

**Was wurde umgesetzt:**
| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | VEB Scout gebaut + analysiert (5 Bars) | `scripts/backtest/veb_scout.py` | TTM-Squeeze: bimodale R-Verteilung, WR=31% — reicht nicht für TP=3R |
| 2 | VEB 3-Bar-Variante getestet | `scripts/backtest/veb_scout.py` | Ökonomisch begründet: kürzere Squeezes = weniger Algo-Hunting; WR verschlechtert sich weiter |
| 3 | ATR-Rider Scout gebaut + analysiert (48H) | `scripts/backtest/atr_rider_scout.py` | Donchian-Entry + Chandelier Trailing Stop; WR=31%, Donchian zu viele Fakeouts |
| 4 | ATR-Rider 96H-Variante getestet | `scripts/backtest/atr_rider_scout.py` | Ökonomisch begründet: 4-Tages-Hochs = komplette Liquiditätszyklen; keine Verbesserung |
| 5 | Anti-Patterns: VEB + ATR-Rider eingetragen | `memory/anti_patterns.md` | Wiederholfehler verhindern, Chandelier-Exit als Reuse dokumentiert |
| 6 | Knowledge Base: Breakout-WR-Ceiling | `memory/knowledge_base.md` | Krypto-1H Breakouts strukturell ~30% WR — Chandelier-Exit als bester Exit dokumentiert |
| 7 | Git-Commit | `b0a423f` | VEB + ATR-Rider scouts archiviert |

**Kern-Erkenntnisse:**
- Krypto-1H-Breakouts (Donchian oder Squeeze) scheitern systematisch an WR~30% — strukturelle Liquidations-Fallen
- Chandelier Trailing Stop (2.5×ATR, nie sinkend) = bester Exit-Mechanismus der Factory (max +13.3R gesehen)
- Nächstes Lab: Chandelier-Exit mit selektivem Entry (VAA-Filter-Idee oder EMA-Crossover) → WR ≥ 40% benötigt

**Hypothesen:** keine neuen H-IDs (beide als NO-GO dokumentiert)
**Commits:** `b0a423f` (VEB + ATR-Rider Scouts)

### 2026-04-24 — Drei Lab-Runs: PLS, SSR, FRF (alle NO-GO)

**Was wurde umgesetzt:**
| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | PLS Scout gebaut + analysiert | `scripts/backtest/pls_scout.py` | Wick-Filter selektiert echte Setups, WR=29% reicht nicht für TP=3R |
| 2 | SSR Scout gebaut + analysiert (BTC/ETH + SOL/AVAX) | `scripts/backtest/ssr_scout.py` | BTC/ETH Regime-Drift; SOL/AVAX vor Fees positiv, 4× Taker-Fees vernichten Edge |
| 3 | FRF Daten-Audit (kein Scout nötig) | `data/funding/` | Bitget Funding-Cap ±0.01% < Taker-Fees 0.12% — strukturell unmöglich |
| 4 | Anti-Patterns: PLS + SSR + FRF eingetragen | `memory/anti_patterns.md` | Wiederholfehler verhindern |
| 5 | Knowledge Base: Bitget-Derivate-Schranken | `memory/knowledge_base.md` | Funding-Cap + Pairs-Trading-Fees als Factory-Regel dokumentiert |

**Kern-Erkenntnisse:**
- PLS: Bimodale R-Verteilung beweist funktionierendes Entry — WR-Problem, kein Mechanik-Problem
- SSR: Pairs Trading auf Bitget strukturell durch Taker-Fees disqualifiziert (4× pro Runde)
- FRF: Bitget-Funding-Cap ±0.01% macht alle Funding-Strategien mathematisch negativ
- Factory-Regel etabliert: Avg Win > 1.5R Mindestanforderung für Bitget-kompatible Strategien
- P-Hacking konsequent abgelehnt (PLS post-hoc Asset-Filter, SOL/AVAX Quick-Fix)

**Hypothesen:** keine neuen H-IDs (alle drei als NO-GO dokumentiert)
**Commits:** `16fea34` (PLS + SSR + FRF Scouts + Memory-Updates)

### 2026-04-24 — KDT Deploy + Telegram-Upgrade + ZVA Lab (NO-GO)

**Was wurde umgesetzt:**
| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | KDT Config-Block + Cron + erster Dry-Run | `config/bot_config.py`, Crontab | KDT-Migration aus Phase 0-5 abschließen |
| 2 | Standardisiertes Event-Tagging | `scripts/telegram_sender.py` | `format_event_tag()` für alle Bots einheitlich |
| 3 | Tags in KDT + VAA Nachrichten | `scripts/kdt_bot.py`, `scripts/vaa_bot.py` | SIGNAL/ENTRY mit Bot-Name + Timestamp |
| 4 | Daily Hedge Fund Briefing | `scripts/daily_briefing.py` | Multi-Bot-Überblick täglich 07:00 UTC |
| 5 | ZVA Scout 1H gebaut + analysiert | `scripts/backtest/zva_scout.py` | /Lab + /Build LONG-only: n=10, +0.826R, p=0.22 |
| 6 | ZVA Scout auf 15m portiert + analysiert | `scripts/backtest/zva_scout.py` | Weg A: n=51, −0.749R — Signal-Frequency-Paradoxon |
| 7 | ZVA NO-GO dokumentiert | `memory/anti_patterns.md`, `memory/knowledge_base.md` | Erkenntnisse sichern, Wiederholung verhindern |

**Kern-Erkenntnisse dieser Session:**
- KDT Forward-Testing läuft (DRY RUN, stündlich, 0/10 Signale)
- Telegram: alle Bots mit einheitlichem `[ APEX · BOT · EVENT · HH:MM ]` Tag
- ZVA-Diagnose: SHORT-Pullbacks in Krypto = Anti-Pattern (Kaskaden/V-Shapes). LONG-1H hat Edge-Andeutung (+0.826R) aber zu selten. 15m zerstört Edge (Rauschen).

**Hypothesen:** KDT (H-200ff implizit via Forward-Test), ZVA endgültig geschlossen
**Commits:** `6c6a546` (KDT Deploy), `7f2e44e` (Telegram-Upgrade)

### 2026-04-24 — KDT Bot deployed (DRY RUN, Forward-Testing)

**KDT (Kinetic Deceleration Trap) — ETH SHORT-only, 1H:**
- Phase 0-5 durchlaufen: 4/6 Hard-Gates (DSR + Bootstrap offen wegen n=17)
- IS: n=17, AvgR=+0.450R, WR=41%, PF=1.64
- OOS: n=4, AvgR=+0.824R, WR=50%, PF=2.48
- Edge: 3 grüne Kerzen mit schrumpfendem Body+Vol über EMA(50) → Sell-Stop am Low
- `kdt_bot.py` läuft stündlich (Cron: `0 * * * *`), DRY RUN
- Finale Validierung (DSR + Bootstrap) nach n≥30 Forward-Test-Signalen geplant

### 2026-04-24 — VAA Asset-Universe-Scan (15 Kandidaten)

**Ergebnis: Kein neuer KEEP — originales Universum bleibt**
- 15 neue Kandidaten gescannt: BTC, XRP, BNB, OP, ARB, INJ, NEAR, APT, TIA, WIF, BONK, PEPE, JUP, SEI, LDO
- Alle TOXIC oder NEUTRAL (zu wenig Signale, n<10 für VAA-Parameter)
- BONK existiert nicht auf Bitget USDT-Futures
- SEI/NEAR NEUTRAL aber statistisch nicht auswertbar (n=2 bzw. n=4)
- `VAA_BLACKLIST` auf 14 Assets erweitert
- `VAA_ASSETS` unverändert: SOL/AVAX/DOGE/ADA/SUI/AAVE
- ORB auf DRY_RUN=True gesetzt (temporär während Asset-Expansion)

### 2026-04-22 — Quant Factory Struktur + VAA Live-Deployment

**VAA-Strategie vollständig validiert (11/11 Hard-Gates):**
- OOS: AvgR=+1.47R, WR=64%, PF=4.63, DSR=0.97
- Assets: SOL/AVAX/DOGE/ADA/SUI/AAVE (ETH/LINK toxisch)
- F-06 ATR-Expansion ist der Key-Filter (+0.59R Delta)
- `vaa_bot.py` läuft seit heute (DRY RUN), Cron: `0 * * * *`

**Infrastruktur erweitert:**
- `bot_status.py` gebaut — auto-detektiert alle Bots via `data/*_trades.json`
- `~/.claude/commands/Lab.md` — `/Lab`-Command für systematische Strategie-Evaluierung
- `CLAUDE.md` neu strukturiert für Quant Factory (10+ Bots)

**`--kill --confirm` Flag** in `bot_status.py` für nicht-interaktiven Panic-Button (Claude-kompatibel)

