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

VAA geht auf LIVE wenn: 10 DRY-RUN-Signale ohne Anomalie + manuelle Freigabe durch Andre.

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

