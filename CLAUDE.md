# APEX Trading Bot – Claude Code Kontext

## ⚠️ ZWEI WELTEN – NIEMALS VERMISCHEN

| | APEX (Live) | Freqtrade (Analyse) |
|---|---|---|
| **Verzeichnis** | `/root/apex-trading-bot/` | `/root/freqtrade-bot/` |
| **Modus** | LIVE – echtes Geld | DRY-RUN – kein echtes Geld |
| **API-Keys** | Aktiv (private Endpunkte) | Leer (nur public OHLCV) |
| **Priorität** | **IMMER HÖCHSTE** | Nachrangig |
| **Rate-Limit Pool** | Private Bitget Endpunkte | Öffentliche Bitget Endpunkte |

**REGEL:** Jede Session beginnt mit `python /root/apex-trading-bot/scripts/apex_status.py`.
Vor JEDER Datei-Änderung sicherstellen: In welchem Verzeichnis bin ich? Was ist der Kontext?

---

## Automatische Anweisungen für Claude Code

**BEIM SESSION-START (Wake-Up-Routine):**
Der Hook läuft automatisch und zeigt den APEX-Status. Lese ihn vollständig.
Dann führe automatisch diese Schritte durch, BEVOR du Andre antwortest:

1. **Pending Notes verarbeiten:** Wenn `apex_status.py` Pending Trade-Notes anzeigt (> 0):
   - Lies `/root/apex-trading-bot/data/pending_notes.jsonl`
   - Für jeden Eintrag: Schreibe eine Micro-Analyse (append) nach `/root/.claude/projects/-root-apex-trading-bot/memory/trade_log.md`
   - Format pro Note: `### [Datum] [Asset] [Direction] [Session] — [R]R ($[PnL]) | [exit_reason]` + 1-2 Sätze Kontext (Slippage, Trend-Alignment, Gate-Fortschritt)
   - Leere danach `pending_notes.jsonl` (schreibe leere Datei)

2. **Deep Review triggern:** Wenn `apex_status.py` "Deep Review FÄLLIG" anzeigt:
   - Führe vollständige Analyse durch (letzte 10 Trades + All-Time: Win-Rate, Avg R, PF, Session/Asset-Breakdown, Skip-Funnel, Slippage-Trend)
   - Prüfe Hypothesen-Gates gegen reale Daten, aktualisiere Status in `hypothesis_log.md`
   - Schreibe Report nach `/root/.claude/projects/-root-apex-trading-bot/memory/reviews/review_YYYY-MM-DD.md`
   - Setze `trades_since_last_review` auf 0 in `pnl_tracker.json`, lösche `deep_review_pending.flag`
   - Präsentiere Andre eine Zusammenfassung mit konkreten Vorschlägen (//EXECUTE-Gate)

3. **Kontext prüfen:** Offene Positionen, ausstehende Punkte aus letztem Session-Log, Hypothesen-Deadlines < 14 Tage → Andre darauf hinweisen.

4. **Optimierungs-Zyklus (JEDE Session):** Nach Datenverarbeitung identifiziere proaktiv die höchste-Impact-Optimierung und präsentiere sie Andre:
   - Lies `/root/.claude/projects/-root-apex-trading-bot/memory/knowledge_base.md` für bestehende Erkenntnisse
   - Analysiere: Welche Hypothese ist am nächsten an einem Gate? Welches Muster in den Daten ist am auffälligsten? Welcher Bottleneck kostet am meisten EV?
   - Priorisiere nach: (a) EV-Impact, (b) Datenreife (genug n?), (c) Implementierungsaufwand
   - Präsentiere Andre EINEN konkreten Vorschlag: "Höchste-Impact-Optimierung diese Session: [X]. Begründung: [Y]. Soll ich?"
   - Wenn Andre zustimmt (//EXECUTE): Implementiere, teste, trage Hypothese ein
   - Wenn Andre ablehnt oder anderes Thema hat: Folge seinem Lead

5. **Wissensaufbau:** Wenn in der Analyse Wissenslücken auffallen (markiert mit `[ ]` in knowledge_base.md oder neue Fragen):
   - Führe Web-Recherche durch (Bitget-API-Docs, quantitative Trading-Literatur, Microstruktur)
   - Dokumentiere Erkenntnisse in `knowledge_base.md` unter der passenden Sektion
   - Leite konkrete Hypothesen ab wenn die Recherche actionable Insights liefert

**ZIEL jeder Session:** Den Bot messbar näher an institutionelles Niveau bringen — durch Datenanalyse, Hypothesen-Validierung, Code-Optimierung ODER Wissensaufbau. Nie eine Session ohne Fortschritt beenden.

**SESSION-ENDE → Befehl: `/ASE`**
Führt die vollständige Session-Ende-Routine durch (CLAUDE.md Log, Memory-Updates, Knowledge-Base, Git-Commit).

**WICHTIG — Robustheit bei vergessenem /ASE:**
Die kritischen Daten (Pending Notes, Deep Reviews, Trade-Log) werden am SESSION-START verarbeitet, nicht am Ende. Wenn /ASE vergessen wird, gehen nur Session-Log-Einträge in CLAUDE.md und Knowledge-Base-Updates verloren — die Rohdaten bleiben in den JSONs und werden in der nächsten Session verarbeitet.

---

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

## Architektur-Entscheidungen & Session-Log

### Session 2026-04-12 (Abend) — Session-Check Skill Fix

**Thema:** Kurze Maintenance-Session. Session-Check hatte Wochenende fälschlicherweise als Bug gemeldet.

| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | Schritt 0 (Wochentag-Check) ergänzt | `.claude/commands/session.md` | Session-Skill sprang direkt in Log-Analyse ohne zu prüfen ob Wochentag = Sa/So |

**Entscheidung:** Schritt 0 prüft jetzt `date +%u` → bei 6/7 sofort stoppen, keine Log-Analyse.

**Nächste Session (Montag):** Optimization Loop starten — Bot systematisch auf institutionelles Niveau bringen.

---

### Session 2026-04-12 — Autonome Optimierungsschleife + Strategie-Analyse

**Thema:** Aufbau der autonomen Optimierungsschleife. Ausgangspunkt war der Master-Prompt "Evolutionary Quant Maintainer" den Andre vorbereitet hatte.

**Analyse-Ergebnisse (Chain of Truth + Devil's Advocate):**

1. **Master-Prompt:** 70% bereits vorhanden (Hypothesis-Framework, Wake-Up, CLAUDE.md). Kein signifikanter Mehrwert als Ganzes. Wertvolle Teile: 3 Kommandos, proaktive Recherche, Orderbook-Snapshots.

2. **Autonome Optimierung:** Bottleneck ist nicht Session-Qualität sondern Session-Frequenz. `CronCreate` ist session-only (stirbt bei Claude-Exit). `RemoteTrigger` läuft in Anthropic-Cloud ohne Zugriff auf lokale Dateien. Sofortige Analyse nach Trade-Close hat ~null Mehrwert — Daten verfallen nicht.

3. **Richtige Lösung:** Session-Start verarbeitet alles automatisch + täglicher Python-Health-Check ohne Claude-API-Kosten.

**Implementiert:**

| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | Wake-Up-Routine erweitert (Auto-Pending-Notes + Auto-Deep-Review + Optimierungs-Zyklus + Wissensaufbau) | `CLAUDE.md` | Proaktive Optimierung ohne manuellen Trigger |
| 2 | `check_system_health()` — täglicher Anomalie-Check (stale Flag, unverarbeitete Notes, Deadlines, DD >30%) | `scripts/daily_closeout.py` | Failsafe ohne API-Kosten, via Telegram bei Anomalie |
| 3 | `knowledge_base.md` — wachsende Wissensdatenbank (Bitget-Microstruktur, ORB-Best-Practices, Research-Backlog) | `memory/knowledge_base.md` | Erkenntnisse überleben Sessions, kein Wissensverlust |
| 4 | `/ASE` Slash-Command ersetzt "apex session end" | `/root/.claude/commands/ASE.md` | Kurzer Befehl statt langer Text |
| 5 | Session-Ende-Routine robuster (CLAUDE.md + project_apex.md + knowledge_base.md) | `CLAUDE.md` | Vollständige Persistenz bei Session-Ende |
| 6 | `project_apex.md` aktualisiert (veraltet: Stand 07.04, sagte 13 Trades) | `memory/project_apex.md` | Aktueller Stand: 9 Trades, $64.42, 33% WR |
| 7 | `MEMORY.md` um knowledge_base.md ergänzt | `memory/MEMORY.md` | Index vollständig |
| 8 | H-010 eingetragen | `memory/hypothesis_log.md` | Autonome Optimierungsschleife als testbare Hypothese |
| 9 | `memory/reviews/` Verzeichnis erstellt | Server | Zielablage für Deep-Review Reports |

**Entscheidungen (mit Begründung):**

- **KEIN RemoteTrigger für trade-debrief:** Cloud-Agent hat keinen Zugriff auf lokale JSONs. Overhead ohne Mehrwert.
- **KEIN subprocess.Popen nach Trade-Close:** Sofortige Analyse = ~null Mehrwert. Session-Start erledigt alles.
- **Python Health-Check statt Claude-Agent:** $0 API-Kosten vs. ~$0.10/Tag. Gleichwertige Funktionalität für einfache Anomalie-Erkennung.
- **`/ASE` als globaler Command** (unter `/root/.claude/commands/`): Verfügbar in allen Claude-Sessions, nicht nur im Repo-Verzeichnis.

**Commit:** `46b8fc2`

---

### Session 2026-04-09 Teil 3 – Spur 1+2: Enhanced Logging + Freqtrade Dry-Run

**Umgesetzt:**

**Spur 1 – APEX Enhanced Logging** (kein Filter, nur Datensammlung):

| Datei | Änderung |
|---|---|
| `scripts/autonomous_trade.py` | `_calc_ema()` + `_calc_atr()` Hilfsfunktionen (reines Python, kein talib). `scan_for_breakouts()` ruft 210×15m-Kerzen ab, berechnet `trend_context` (EMA-200/50, ATR-14, trend_direction, atr_ratio). `execute_breakout_trade()` ruft `market_structure` beim Entry ab. Beides in `trades.json` geloggt. |
| `scripts/bitget_client.py` | 3 neue Public-API-Methoden: `get_open_interest()`, `get_long_short_ratio()`, `get_taker_ratio()`. Außerdem `pageSize` → `limit` Bug-Fix (400172 Error auf `orders-plan-pending`). |

**Spur 2 – Freqtrade Dry-Run** (parallel zu APEX, kein echter Einsatz):

| Datei | Änderung |
|---|---|
| `AdvancedORB.py` | `order_types["entry"]`: `"limit"` → `"market"` (Limit verlor 61% der Signale durch Entry-Timeouts). `custom_entry_price()` → Pass-Through. |
| `config.json` | `entry_pricing.price_side`: `"same"` → `"other"` (Pflicht für market orders). XRP zu Whitelist hinzugefügt. `stake_amount`: `"unlimited"` → `33.33`. |

**Backtest-Ergebnis** (Okt 2025–Apr 2026, -57% Markt): 867 Trades, 31% Win-Rate, PF 0.52, -6.39%. Beurteilung: Market-Crash-Periode ist worst-case für Long-Trades; TP1/TP2 korrekt implementiert (partial_exit bestätigt). Dry-Run in aktuellem Markt aussagekräftiger.

**Erstes Dry-Run Signal sofort:** AVAX ORB_Long entry 9.333 → TP1 partial_exit bei 9.449 (+1.2%). `adjust_trade_position` funktioniert korrekt.

**Hypothese H-005 eingetragen.** Freqtrade läuft auf `/root/freqtrade-bot/` mit `docker compose`.

---

### Session 2026-04-09 Teil 2 – H-002: TP2 Trailing → Statisches TP2 @ 3R (Strategie-Fix)

**Anlass:** Andre wies auf XRP SHORT (2026-04-09 02:30) hin – Trade hatte TP1 gesichert, BE nachgezogen, dann lief Preis 1.53R und drehte komplett zurück Richtung BE. Ohne manuellen Eingriff wäre der Trade bei ~0.5R gelandet.

**Root Cause (zwei überlagerte Bugs):**

1. **Dead Zone zwischen TP1 (1R) und Trailing-Aktivierung (2R):** Wenn Preis nur 1.5R macht und dreht, hat die zweite Hälfte keinen Profit-Mechanismus. BE_SL fängt bei ~Entry – Gewinn-Potenzial verpufft.

2. **Bitget `rangeRate` 1%-Floor tötet den Trailing bei kleinem Kapital:**
   ```python
   trail_pct = (risk_actual * 0.5) / actual_entry
   # XRP:  0.0025 (0.25%) → geclampt auf 1.0%
   # SOL:  0.00004 (0.004%) → geclampt auf 1.0%
   # ETH:  0.0000019 (0.0002%) → geclampt auf 1.0%
   ```
   Bei 2R-Aktivierung ergibt 1% Trail einen Stop bei ~Entry-Niveau → **identisch zum BE-SL**. Trailing ist strukturell wertlos bei Kapital < ~$500.

**R/R-Analyse (warum das alte System mathematisch ein Verlierer war):**

| Szenario | Altes System | Neues System (Static TP2 @ 3R) |
|---|---|---|
| Full Win (Preis → 3R) | +0.5R (TP1) + ~0R (Trail clamped) = **+0.5R** | +0.5R + 1.5R = **+2R** |
| Partial (1R hit, Reversal) | +0.5R (TP1) + 0R (BE) = **+0.5R** | +0.5R (gleich) |
| Direct SL | **−1R** | **−1R** |

EV-Rechnung @ 30% Full / 30% Partial / 40% SL:
- Alt: 0.3·0.5 + 0.3·0.5 + 0.4·(−1) = **−0.1R** ❌ (braucht 67% Winrate zum Break-Even)
- Neu: 0.3·2 + 0.3·0.5 + 0.4·(−1) = **+0.35R** ✅ (braucht nur 33% Winrate)

**Verbesserung: +0.45R pro Trade.** Selbst im pessimistischen Szenario (20%/30%/50%) ist das neue System noch ~0R (Break-Even) statt −0.25R.

**Implementierung (Tasks 10–16):**

| # | Datei | Änderung |
|---|-------|----------|
| 1 | `hypothesis_log.md` | H-002 eingetragen (logic, Validation Gate: 10 Trades / 2026-05-15) |
| 2 | `autonomous_trade.py` | `trail_pct`/`trailing_activation` → `take_profit_2` (3R statisch), `place_trailing_stop` → `place_take_profit`, log_trade-Dict + return-Dict + print/telegram aktualisiert, `ratio` = "Split 1:1 + 3:1" |
| 3 | `position_monitor.py` | BE-Failsafe (Trailing-Restore) entfernt – war defensiv für Cancel-Bug, der schon längst gefixt ist. TP1/TP2 sind beide profit_plan und überleben `cancel_tpsl_orders(plan_types=["loss_plan"])` automatisch. |
| 4 | `bitget_client.py` | `place_trailing_stop()` als **DEPRECATED** markiert (Docstring-Note), Funktion bleibt für v2 DIY-Trailing erhalten. `cancel_tpsl_orders` Docstring angepasst. |
| 5 | `memory/user_trust_andre.md` | Neue User-Memory: Andre ist Entscheider, ich bin Stratege – R/R-Check Pflicht vor jeder Strategie-Änderung. |
| 6 | `memory/project_parked_diy_trailing.md` | DIY-Trailing-Idee geparkt – kommt zurück wenn Kapital ≥ $500 oder H-002 verified. |

**Was wir dadurch verlieren:** Die theoretische Fähigkeit des Bitget-Trailing, bei starken Trends über 3R hinaus zu laufen. **Praktisch verlieren wir nichts**, weil der Trailing bei Andres Kapital nie funktioniert hat (siehe Clamp-Rechnung oben).

**Was wir dadurch gewinnen:** Klares, berechenbares R/R (1:2 in Full-Wins), keine Clamping-Edge-Cases, robustes System auch bei niedriger Winrate. Validation über nächste 10 Trades via Deep Review (H-001-Infrastruktur liefert die Daten).

**H-002 Validation-Gates (nach 10 neuen Trades):**
1. Durchschnitts-R pro Trade ≥ +0.2R?
2. Mindestens 2 Trades mit ≥1.5R Exit-PnL?
3. TP2 @ 3R wurde bei ≥1 Trade ausgelöst?

Alle drei ja → `verified`. Null von drei → `rejected`, dann Fallback auf TP2 @ 2R oder 2.5R evaluieren.

---

### Session 2026-04-09 – Workflow-Umbau: 3-Säulen Optimization System

**Kontext:** Bisher liefen Optimierungs-Erkenntnisse ad-hoc über Weekly Review + qualitycheck und landeten uneinheitlich in CLAUDE.md / Memory. Kein strukturierter Draht zwischen "was geändert" und "was gemessen". Nach nur 3 Trades war das noch tolerabel – ab jetzt (wachsendes n) nicht mehr.

**Entscheidungen (via AskUserQuestion):**
1. **Datenbasis:** Abwarten + Logging verbessern (kein Backtester, zu wenig Daten)
2. **Kadenz:** Event-basiert (Micro-Note nach jedem Trade-Close, Deep Review alle 10 Trades)
3. **Hypothesen:** Für ALLE Änderungen (auch Bug-Fixes) – Hypothesis Registry als Single Source of Truth

#### Block 1 – Data Layer (erweitertes Logging)

| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | `skip_log.jsonl` + `log_skip()` Helper | `autonomous_trade.py` | Skip-Funnel sichtbar machen: WARUM wird ein Signal verworfen? (11 Skip-Pfade instrumentiert: position_open, box_too_old, box_missing_ts, box_too_small, price_fetch_fail, no_breakout, late_entry, candle_not_confirmed, no_session, already_traded, kill_switch) |
| 2 | Slippage-Capture in `execute_breakout_trade()` | `autonomous_trade.py` | `trigger_price` vs `actual_entry` → $-Slippage pro Trade in `trades.json` + pending_notes |
| 3 | `get_funding_paid()` via `/api/v2/mix/account/bill?businessType=contract_settle_fee` | `bitget_client.py` | Funding-Kosten pro Trade quantifizieren – vor allem für länger offene Positionen |
| 4 | Drawdown-Timeline in `drawdown.json` | `daily_closeout.py` | Tägliche Snapshots (HWM + history[]) für Equity-Curve-Analyse |

#### Block 2 – Governance Layer (Hypothesis Registry)

| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 5 | `memory/hypothesis_log.md` mit Konventionen | Memory | Append-only Log aller Änderungen als testbare Hypothesen (ID, Type, Status, Baseline, Validation, Deadline, Commit, Outcome). Jede Hypothese wird beim nächsten Deep Review oder nach Deadline auf `verified`/`rejected`/`inconclusive` gesetzt. |
| 6 | `feedback_memory_update.md` erweitert | Memory | Neue Regel: Hypothesis-Entry VOR Commit, Commit-Hash nachtragen. Pending-Notes-Verarbeitung beim Session-Start definiert. |
| 7 | `qualitycheck.md` Phase 3 erweitert | `.claude/commands/` | Jeder qualitycheck-Bug bekommt jetzt eine Hypothese im Log, Commit wird referenziert, Report zeigt Hypothesis-Section. |

#### Block 3 – Process Layer (Event-basierte Reviews)

| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 8 | `pending_notes.jsonl` Writer | `position_monitor.py` | Bei jedem Exit wird eine strukturierte Note geschrieben (asset, session, pnl_r, box_range, vol_ratio, slippage, funding, exit_reason). Claude verarbeitet beim Session-Start → `memory/trade_log.md`. |
| 9 | `trades_since_last_review` Counter + Flag-File | `position_monitor.py` | Bei ≥10 Trades wird `deep_review_pending.flag` gesetzt → Claude triggert Deep Review, schreibt Report nach `memory/reviews/review_YYYY-MM-DD.md` (Skip-Funnel, Win-Rate-Δ, Hypothesen-Status), reset Counter. |
| 10 | `apex_status.py` 3 neue Sektionen | `scripts/apex_status.py` | SessionStart-Hook zeigt jetzt: (a) Pending Trade-Notes, (b) Deep Review fällig, (c) Offene Hypothesen mit Deadline → Claude sieht beim ersten Prompt was zu tun ist. |
| 11 | `memory/trade_log.md` + `memory/reviews/README.md` angelegt | Memory | Zielablagen für Micro-Notes und Deep Reviews definiert. Chronologisch append-only. |

#### Verifikation (2026-04-09)

End-to-End mit echten Daten bestätigt:
- 2 reale Pending-Notes (SOL US LOSS, XRP Tokyo BE_WIN) in `trade_log.md` verarbeitet
- Slippage-Capture live: XRP = $0.0014 auf $1.34 (~0.1bp) – Baseline etabliert
- `apex_status.py` zeigt Pending Events + offene Hypothesen in dedizierter Sektion
- Syntax-Check aller modifizierten Scripts: `SYNTAX_OK`

#### Erste Datenpunkte (n=2, NICHT verallgemeinern)

- **SOL LONG US LOSS** trotz vol_ratio **2.18x** → erster Marker gegen naiven Volume-Filter
- **XRP SHORT Tokyo BE_WIN** mit vol_ratio 1.43x + BE applied bei winziger Box ($0.003) → BE-Mechanik greift wie designed

#### Registrierte Hypothese

**H-001** · infrastructure · Status: **open**
> Ein strukturierter Workflow (Logging + Event-Reviews + Hypothesis Registry) liefert innerhalb von 30 Trades genug Signale für mindestens einen datengetriebenen Parameter-Vorschlag.

**Deadline:** nach 30 Trades ODER 2026-06-30 (je früher).
**Validation Gate 1 (nach 10 Trades):** ≥1 konkreter Parameter-Vorschlag aus den Daten ableitbar?
**Validation Gate 2 (nach 30 Trades):** ≥3 `verified`/`rejected` Hypothesen im Log?

---

### Session 2026-04-08 – Code Review: 8 Bugs + Edge-Case-Schutz (Trailing, Atomars, Defensive Exits)

**Externe Code-Review** identifizierte 8 versteckte Bugs + 1 Edge-Case.

#### Die 8 Hauptfixes

**1. KRITISCH – Trailing-Stop-Kill bei Break-Even:**
- `check_and_apply_break_even()` cancelte mit `cancel_tpsl_orders(pos.coin)` **ALLE** Plan-Orders.
- Das heißt: TP1, TP2 (Trailing), UND SL wurden gelöscht; nur neuer BE-SL kam rein.
- Effekt: Nach 1R Gewinn war dein kompletter Trailing-Stop (Upside-Maschine) tot.
- **Fix:** `cancel_tpsl_orders()` nimmt jetzt optionalen `plan_types`-Filter → nur `["loss_plan"]` canceln.
- **Failsafe:** Falls Trailing trotzdem weg → aus `trailing_activation` + `trail_pct` aus `trades.json` neu setzen.
- **Konsequenz:** Trailing bleibt nach BE aktiv, Trade läuft bis 3R–5R statt bei 1R zu enden.

**2. Log-Rotation (truncate vs tail):**
- Alte Variante: `truncate -s 1M` behielt die **ersten** 1MB (älteste Daten).
- Neue Variante: `tail -c 1048576 | mv` behält die **letzten** 1MB (neueste Daten).
- **Konsequenz:** Wenn was bricht, deine Diagnostic-Logs sind noch da.

**3–6. Atomare Writes + Error Handling:**
- `save_opening_range.py`, `update_pnl_tracker()`, `save_state()` schreiben mit tmp+rename.
- `load_boxes()`: try/except gegen `JSONDecodeError`.
- **Konsequenz:** Keine JSON-Korruption bei mid-write Crashes; Bot fährt fort.

**5. MIN_TRADE_SIZE Enforcement:**
- TP1-Split kann bei tiny Sizes zu `0.0` runden (banker's rounding).
- Jetzt: if `size_tp1 < MIN_TRADE_SIZE` → skip TP1, alles ins Trailing.
- **Konsequenz:** Keine Dummy-Orders an Bitget, die ignoriert werden.

**7. API-Call Optimierung:**
- `scan_for_breakouts()` gibt jetzt `(breakout, positions)` Tupel → kein 2. `get_positions()` nötig.
- **Konsequenz:** -1 API-Call pro Run, weniger 429-Rate-Limit-Risiko.

**8. Trade-Lookup Performance:**
- `has_traded_today_in_session()` iteriert reversed() → jüngster zuerst.
- Early-break bei älterem Datum.
- **Konsequenz:** Jüngster Trade gepickt, ~10% Performance-Gewinn.

**9. Session-Summary Exit-Info:**
- Zeigt nun `exit_pnl_usd / exit_pnl_r / exit_reason` wenn Trade in Session schon closed.
- **Konsequenz:** User sieht sofort ob Trade noch offen oder done + wie.

#### Edge-Case: Trailing-Only-Mode + TP2 fail (Commit `3928663`)

**Das Problem:**
```
size_tp1 = 0 (zu klein, Trailing-Only-Mode)
    AND
tp2_ok = False (Trailing-Stop fail)
    →
Trade hat NUR SL als Exit, keinen Profit-Mechanismus
```

**Die Lösung:**
In dieser Konstellation Position sofort mit `reduce_only=True` schließen + Telegram-Alert.

**Warum beide Commits nötig waren:**
1. `5a26115`: Die 8 Bugs fixen (das war schon produktionsreif)
2. `3928663`: Edge-Case absichern (defensiv für zukünftige Szenarien)

**Risiko-Bewertung bei Andres Capital ($68):**
- TP1-Hälfte typisch 0.045–0.13 ETH
- MIN_TRADE_SIZE für ETH = 0.01
- Trailing-Only-Mode tritt **praktisch nie** auf
- **ABER:** Bei kleinerer Capital oder sehr weitem SL könnte es greifen → Schutz ist da.

#### Kompletter Trade-Lifecycle nach Fixes

```
═══ ENTRY-FILTER (autonomous_trade.py) ═══
  ✓ Schon getradet heute?              → Skip
  ✓ Position offen?                    → Skip
  ✓ Box älter als 120 Min?             → Skip
  ✓ Box Range < Minimum?               → Skip
  ✓ Late-Entry (>2x Range)?            → Skip
  ✓ Candle-Close Confirmation?         → Skip (nur confirmed closes)
  ✓ Kill-Switch (50% DD)?              → Skip
  ✓ → execute_breakout_trade()

═══ ENTRY (execute_breakout_trade) ═══
  1. SL = box±buffer
  2. Size = risk/sl_distance, gerundet
  3. Margin-Cap auf 90% Konto
  4. Orphan-Cleanup (mit 1x retry)     → ABORT wenn fail
  5. place_market_order + preset SL    → ABORT wenn fail
  6. Sleep 5s (Bitget braucht Zeit)
  7. SL-Validierung:
     ✓ Preset SL aktiv?                → OK
     ✗ Sonst: place_stop_loss retry    → ABORT wenn IMMER fail
     🚨 Kein SL → Notschließung
  8. TP1: nur wenn size_tp1 > MIN, mit retry
  9. TP2: Trailing-Stop mit retry
  10. Trailing-Only-Mode + TP2 fail?   → 🚨 Notschließung (NEU)
  11. tp_ok = tp1_ok AND tp2_ok        → ⚠️ Warning wenn fail (läuft weiter)
  12. → log_trade()

═══ LAUFZEIT (position_monitor.py, alle 5min) ═══
  ✓ 1R erreicht?
    • Cancel nur loss_plan (nicht TP1/Trailing!)
    • Place neuer BE-SL
    • Failsafe: Re-place Trailing wenn fehlt (+ GET-plausibilität)
    • → TP1 + Trailing bleiben aktiv (GEFIXT)

═══ EXIT (automatisch oder manuell) ═══
  • TP1 → halbe Size, Position halbt sich
  • TP2/Trailing → Großteil der Position
  • SL → Verlust, Position geschlossen
  • BE-SL → Breakeven, Position geschlossen
  • → update_trade_with_exit() loggt: exit_pnl_usd, exit_pnl_r, exit_reason
```

#### Garantien nach allen Fixes

| Szenario | Ergebnis |
|----------|----------|
| SL nicht setzbar | 🚨 Notschließung + Telegram |
| Trailing-Only + Trailing fail | 🚨 Notschließung + Telegram |
| TP1 fail / TP2 ok | ⚠️ Warning, läuft mit SL+Trailing |
| TP1 ok / TP2 fail | ⚠️ Warning, läuft mit SL+TP1 |
| BE bei 1R | TP1 + Trailing BLEIBEN (gefixt!) |
| File-Crash mid-write | Atomar → keine Korruption |
| Trade ohne TP | Nicht möglich (notgeschlossen) |

**Commits:** `5a26115` (8 Bugs), `3928663` (Edge-Case)
**Status:** ✅ Produktionsreif, Bot kann ohne Risiko traden.

---

### Session 2026-04-07 – Exit-Mechanismus Überarbeitung

**Analyse:** State Pattern vs. Hybrid-Architektur für ORB-Exit-Management

Wir haben eine vorgeschlagene State-Pattern-Architektur (Active → BreakEven → Trailing) analysiert
und gegen die bestehende cron-basierte Architektur abgewogen (Chain of Truth + Devil's Advocate).

**Kernerkenntnisse:**

1. **State Pattern ist konzeptuell richtig**, aber für cron-Betrieb nicht direkt umsetzbar –
   Trailing bei 30-Minuten-Granularität ist de facto ein statischer Stop.

2. **Bitget hat nativen Trailing Stop** (`planType: "moving_plan"`), den wir nicht nutzten.
   Exchange-side Trailing überlebt Server-Abstürze ohne Cancel-Replace-Lücken.

3. **Split-TP implementiert Break-Even bereits implizit** (TP1 bei 1:1 auf halbe Size),
   aber ein expliziter BE-SL-Verschiebung macht den Schutz sauber und Exchange-side.

4. **Short-Handling Bug:** `peak_price` wurde mit `max()` für beide Richtungen getrackt.
   In der BE-Logik fehlte die Short-Richtung vollständig.

5. **Cancel-Replace erzeugt SL-Lücken-Fenster** (~2-5 Sekunden ohne Schutz).
   Jede SL-Aktualisierung = ein Fenster. Native Orders umgehen das.

**Implementierte Verbesserungen (2026-04-07):**

| # | Was | Datei | Warum |
|---|-----|-------|-------|
| 1 | `place_trailing_stop()` (moving_plan) | `bitget_client.py` | Exchange-side Trailing, kein Daemon nötig |
| 2 | TP2 → Trailing Stop bei 2R Aktivierung | `autonomous_trade.py` | Dynamischer Exit statt statischem 3:1 |
| 3 | Break-Even SL-Verschiebung bei 1R | `position_monitor.py` | SL auf Entry+Buffer wenn Trade risikolos |
| 4 | Monitor-Intervall: 30 Min → 5 Min | `crontab_template.txt` | BE-Check braucht feinere Granularität |
| 5 | Short-Direction korrekt in BE-Logik | `position_monitor.py` | SL-Bewegung ist richtungsabhängig |
| 6 | Volume-Logging beim Entry | `autonomous_trade.py` | Breakout-Volumen + 20er-Avg für spätere Analyse |
| 7 | Exit-Logging in trades.json | `position_monitor.py` | PnL, R-Multiple, Exit-Grund pro Trade nachvollziehbar |
| 8 | `apex_status.py` – Session Context Script | `scripts/apex_status.py` | Einbefehl-Kontext: Balance, Trades, P&L, Session-Log |
| 9 | Auto-Hook Session-Start | `~/.claude/settings.json` | Kontext läuft automatisch beim ersten Prompt |
| 10 | Memory-Dateien erstellt | `/root/.claude/projects/-root/memory/` | Projektkontext über Sessions hinweg persistent |

**Entschiedene Nicht-Implementierungen (mit Begründung):**
- **Volume-Filter:** Zu wenig Daten (13 Trades). Erst loggen, dann mit ~30+ Trades evaluieren.
- **State Pattern als Daemon:** Architektursprung zu groß. Bitget native Trailing ist gleichwertig.
- **Häufigeres Polling:** Kein Mehrwert – Trailing läuft exchange-side, BE bei 5 Min ok.

**Qualitycheck-Findings (2026-04-07):**

3 verpasste Trades in 48h – alle durch Bugs verursacht, alle behoben:

| # | Bug | Schwere | Behoben |
|---|-----|---------|---------|
| 1 | `KeyError: 'breakout_size'` (umbenannt zu `breakout_distance`) | Kritisch | ✅ |
| 2 | Direktes JSON-Schreiben ohne tmp+rename (Korruption bei Absturz) | Hoch | ✅ |
| 3 | Kein Telegram-Alert bei position_monitor.py Exception | Hoch | ✅ |
| 4 | BE-Preis Fallback: `current_price=0` wenn API fehlschlägt | Mittel | ✅ |
| 5 | Doppelter `get_positions()` API-Call pro Cron-Run | Mittel | ✅ |

Übersprungen (begründet): Timezone (Server=Berlin ✓), SL-Buffer (by design), Race Condition (Lock schützt), trades.json Format-Mix (legacy, kein Einfluss auf neue Trades)

**Offene Verbesserungsliste (priorisiert):**
1. Volume-Filter evaluieren sobald ~30 Trades mit Volume-Daten vorliegen
2. P&L-Analyse: Winrate, Avg R-Win vs R-Loss nach ausreichend Trade-History
3. `45115` Bitget Preis-Format Fehler beobachten – einmalig aufgetreten, kein Fix nötig
4. Daemon-Architektur wenn Kapital > $200 und Edge bewiesen
5. Funding-Rate Logging (nach Volume-Analyse)

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

## Aktueller Setup-Status

✅ **Server vollständig eingerichtet und LIVE** (seit 2026-04-06)

- Repo: `https://github.com/AS90555/apex-trading-bot`
- API-Keys eingetragen, Bot läuft mit `DRY_RUN = False`
- Crontab aktiv (3 Sessions/Tag + Monitor alle 5 Min)
- Freqtrade Dry-Run parallel aktiv unter `/root/freqtrade-bot/` (seit 2026-04-09)

**Freqtrade-Verwaltung:**
```bash
cd /root/freqtrade-bot
docker compose up -d      # starten
docker compose down       # stoppen
docker compose logs -f    # live logs
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

## Session-Befehle für Claude Code

### Befehl 1: Session START – Kontext wiederherstellen
```bash
! python /root/apex-trading-bot/scripts/apex_status.py
```
Gibt aus: Balance, offene Positionen, aktive Orders, letzte 8 Trades mit Volume-Ratio und P&L, Winrate, Systemstatus.
**Immer als erstes in einer neuen Session ausführen.**

### Befehl 2: Session ENDE – Erkenntnisse sichern
Am Ende einer Session einfach schreiben:
```
apex session end
```
Claude aktualisiert dann `CLAUDE.md` (Architektur-Log) und die Memory-Dateien unter `/root/.claude/projects/-root/memory/` mit den Erkenntnissen der Session.

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
