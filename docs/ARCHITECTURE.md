# Multi-Agent Algorithm Trading System (MATS)
## Orchestriert via Paperclip · OpenCode Zen Free-Modelle (primär) · LM Studio (offline Fallback)

> **Ziel:** Passive, capital-effiziente Einkünfte über Aktien (Alpaca), Forex (Alpaca),
> Krypto-Spot & -Perpetuals (BingX, Bitunix) mit minimalem Kapitaleinsatz
> (~500 € Startkapital) und maximaler Automatisierung.
>
> **Wichtig:** Alle intensiven LLM-Aufgaben laufen auf **kostenlosen OpenCode Zen
> Free-Modellen** (Big Pickle, Nemotron 3 Ultra/Super, MiMo V2.5 Flash, MiniMax M2.5,
> LongCat 2.5, Space Bunny, GPT-5 Nano). LM Studio mit lokalen Q4-Modellen dient
> **nur** als Offline-Fallback bei Netzausfall.

---

## 1. Systemarchitektur

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            BOARD (Mensch)                                    │
│                Setzt Limits, startet/stoppt, liest Berichte                  │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               │  Paperclip Dashboard http://localhost:3100
┌──────────────────────────────▼───────────────────────────────────────────────┐
│                        PAPERCLIP CONTROL PLANE                               │
│       Org Chart · Tasks · Budgets · Heartbeats · Approvals · Audit Log       │
│                            PostgreSQL (embedded)                             │
└──────┬────────┬────────┬────────┬────────┬────────┬───────────────────────────┘
       │        │        │        │        │        │
 ┌─────▼──┐┌───▼──┐┌────▼────┐┌──▼────┐┌──▼─────┐┌─▼───────────┐
 │ CEO    ││Cost  ││Res.    ││Back- ││Risk    ││Execution    │
 │opencode││Opt   ││Agent   ││test  ││Agent   ││Agent        │
 │_local  ││(opc  ││(openc  ││(proc)││(openc  ││(prozess-    │
 │Neotone ││call) ││ode)    ││(num) ││ode)    ││determinist.)│
 │3 Ultra ││MiMo  ││Big     ││      ││MiniMax ││KEIN LLM     │
 │Free    ││Flash ││Pickle  ││      ││M2.5    ││             │
 └────┬───┘└──┬───┘└───┬────┘└──┬───┘└───┬────┘└──────┬──────┘
      │       │        │       │        │             │
      └───────┴────┬───┴───────┴────────┘             │
                   │                                  │
         ┌─────────▼──────────┐  ┌────────────────────▼───────┐
         │ Shared State       │  │  Python Execution Engine    │
         │ (SQLite/JSON Files)│  │  (scripts/ – pandas/ta/…)   │◄── Broker APIs
         └────────────────────┘  └─────────┬──────────────────┘    (Alpaca/BingX/Bitunix)
                                           │
                                  ┌────────▼─────────┐
                                  │ OpenCode CLI     │
                                  │ (free Zen tier)  │
                                  │  opencode.ai     │
                                  └────────┬─────────┘
                                           │ offline?
                                  ┌────────▼─────────┐
                                  │ LM Studio (lokal)│ ← Fallback bei
                                  │ Q4_K_M Modelle   │   Netzausfall
                                  └──────────────────┘
```

### Modellzuordnung – Agent → Free-Modell (OpenCode Zen)

| Agent | Free-Modell (primär) | Grösse/Kontext | Begründung | Offline-Fallback (LM Studio) |
|-------|----------------------|----------------|------------|-------------------------------|
| **CEO** | `opencode/nemotron-3-ultra-free` (Neotone 3 Ultra Free / Nemotron 3 Ultra 550B) | 204k ctx | Stärkstes verfügbares Free-Modell; komplexes Reasoning, lange Berichte, Portfolio-Analysen über viele Daten | `qwen2.5-7b-instruct-q4_k_m` (~4.7GB) |
| **Research** | `opencode/big-pickle` | 200k ctx | Coding/Agent-mystery-Modell, gut bei Mustererkennung auf JSON-Daten, Tool-Calling | `llama-3.2-3b-instruct-q4_k_m` (~2GB) |
| **Risk/CRO** | `opencode/minimax-m2.5-free` | 200k ctx | Starke Code/Logik-Performance, zuverlässiges JSON, lange Kontexte für Portfolio-Daten | `llama-3.2-3b-instruct-q4_k_m` |
| **Backtest (Review)** | `opencode/gpt-5-nano` | kompakt | Schnelles kleines Modell für kurze Plausibilitäts-Kommentare | `qwen2.5-1.5b-instruct-q4_k_m` |
| **Cost Optimizer** | `opencode/mimo-v2.5-flash-free` | 16k ctx | Xiaomi-Schnellmodell für kurze Zahlen-Auswertungen | `qwen2.5-1.5b-instruct-q4_k_m` |
| **Execution** | **KEIN LLM** | – | Deterministische Python-Logik (keine Halluzinationsgefahr bei Orders) | – |
| **Backtest (Engine)** | **KEIN LLM** | – | Reine Numerik (pandas/numpy) | – |

### Sekundär/Backup-Modelle auf OpenCode Zen (für Load-Balancing/Ausfall)
- `opencode/space-bunny-free` – Zero-Retention, Privacy-fokussiert (z.B. für heikle Portfolio-Daten)
- `opencode/longcat-2.5-preview-free` – 200k ctx für besonders lange Analysen (z.B. CEO Monatsbericht)
- `opencode/nemotron-3-super-free` – 120B NVIDIA, Alternative falls Ultra nicht erreichbar
- `opencode/nemotron-3.5-lightning-free` – Schnelleres NVIDIA-Modell für einfache Tasks
- `opencode/mimo-v2-pro-free` – Stärkeres Xiaomi-Modell (Coding), Reserve für Research

### Warum OpenCode Free primär statt lokal LM Studio?
1. **Kein RAM-Druck auf dem N150:** Dein 4C/16GB-System wird nicht von 7B-Modellen blockiert.
   Die Free-Modelle laufen in der Cloud, lokale Ressourcen bleiben für Data-Pipelines.
2. **Bessere Modellqualität:** Nemotron 3 Ultra (550B Parameter!) und MiniMax M2.5 sind
   einem lokalen 7B-Modell weit überlegen – besser für komplexe Finanzentscheidungen.
3. **Keine Kosten:** Alle Modelle sind zum Zeitpunkt der Einrichtung kostenlos im
   OpenCode Zen-Free-Tier verfügbar.
4. **Privacy-Option:** Space-Bunny-Free mit Zero-Retention für sensible Analysen.
5. **Robustheit:** Wenn das Internet ausfällt, greift der LLM-Client **automatisch**
   auf das lokale LM Studio zu (keine harte Downtime).

### Wann LM Studio verwendet wird
- Manueller Betrieb ohne Internet (CachyOS offline)
- Wenn OpenCode Free-Tier-Limits erreicht oder TLS/Netzwerk-Fehler auftreten
- Für extrem schnelle, einfache Abfragen wo Cloud-Latenz stört
- Kostenlose Probe-/Backup-Phase

---

## 2. Harness-Auswahl & Konfiguration

### Harness-Architektur
| Schicht | Technologie | Begründung |
|---------|-------------|------------|
| **Orchestrierung** | Paperclip (npx paperclipai) | Org-Chart, Budgets, Routinen, Approvals, Audit-Dashboard |
| **LLM-Agenten (CEO, Research, Risk, Backtest-Review, Cost)** | `opencode_local`-Adapter | OpenCode CLI mit Zen-Free-Anbieter; jeder Agent bekommt sein eigenes Modell via `--model`-Flag |
| **Deterministische Worker (Execution, Backtest-Engine)** | `process`-Adapter | Reine Python-Prozesse, kein LLM, schnelle deterministische Ausführung |
| **LLM-Client für Worker** | `scripts/common/llm.py` | Wrapper: primär `opencode run` per CLI, Fallback auf LM Studio HTTP-API |
| **Lokaler Fallback** | LM Studio Local Server (Port 1234) | Nur bei OpenCode-Ausfall; GGUF Q4_K_M Modelle auf CPU |

### OpenCode-Konfiguration (`/home/user/zhf/.opencode/opencode.json`)
Die zentrale Konfiguration definiert einen `opencode`-Provider mit allen Free-Modellen
und einen `lmstudio`-Provider als Fallback. Siehe `.opencode/opencode.json` im Repo.

### Adapter-Typen in Paperclip
| Agent | adapter_type | Command/Config |
|-------|--------------|----------------|
| CEO | `opencode_local` | `--model opencode/nemotron-3-ultra-free`, CWD=/home/user/zhf, System-Prompt aus `prompts/ceo.md` |
| Research | `opencode_local` | `--model opencode/big-pickle` |
| Risk | `process` | `/home/user/zhf/.venv/bin/python -m scripts.risk.run` (ruft intern opencode auf) |
| Backtest | `process` | `/home/user/zhf/.venv/bin/python -m scripts.backtest.run` |
| Execution | `process` | `/home/user/zhf/.venv/bin/python -m scripts.execution.run` (KEIN LLM) |
| Cost | `process` | `/home/user/zhf/.venv/bin/python -m scripts.cost.run` |

> **Hinweis:** Paperclips `opencode_local`-Adapter spawnt selbst OpenCode. Für CEO
> und Research nutzen wir diesen direkten Adapter. Für die Worker-Prozesse (Risk,
> Cost, Backtest-Review) rufen wir `opencode run` via Python-Subprocess auf, damit
> wir den Aufruf kontrolliert in unsere Pipeline (JSON-Validierung, Retry,
> Fallback) einbetten können.

---

## 3. Datenfluss & Heartbeat-Rhythmen

```
Heartbeat-Schedule (Paperclip Routines)
│
├─ 30m ──► Research (opencode/big-pickle) ──► candidates.json
│                                              │
├─ 15m ──► Backtest (process, kein LLM) ◄─────┘
│            │
│            └─► validated.json
│                 │
├─ 5m ──► Risk (process → opencode/minimax-m2.5-free) ◄── fee_report.json
│           │
│           └─► approved.json
│                 │
├─ 1m (market) ──► Execution (process, KEIN LLM) ──► Broker APIs
│                    │
├─ 1h ──► Cost Opt (process → opencode/mimo-v2.5-flash-free)
│
├─ 10m ──► Watchdog (process) – Health + TLS-Checks
│
└─ EOD ──► CEO (opencode/nemotron-3-ultra-free) ──► daily_report.md → Board
```

---

## 4. Marktabdeckung

| Markt | Broker | Asset-Typ | Kapital-Effizienz | Strategien |
|-------|--------|-----------|-------------------|------------|
| US-Aktien | Alpaca (Paper→Live) | Einzelaktien, ETFs (fraktional) | Ab $1 pro Position | Swing/Mean-Reversion/EMA |
| Krypto-Spot | Alpaca Crypto | BTC/USD, ETH/USD, SOL/USD | Kein Hebel | Trend/DCA |
| Krypto-Perps | BingX | USDT-M Perpetuals | Max 3x Hebel ( konservativ ) | Breakout/Funding |
| Krypto-Perps | Bitunix | USDT-M Perpetuals | Max 3x Hebel | Breakout (Diversifikation) |
| Forex (später) | Alpaca/Oanda | Majors (EUR/USD, GBP/USD) | 1:5 Hebel max | Range/Carry |

### Kapitalallokation (Start 500 €)
- 250 € Aktien (fraktional, min $5/Position)
- 100 € Krypto-Spot
- 100 € Krypto-Futures (max 3x → effektiv 300 € Exposition, aber auf 100 € Margin begrenzt)
- 50 € Cash-Puffer

---

## 5. Fehlerbehandlung & Resilienz

| Fehler | Reaktion |
|--------|----------|
| OpenCode TLS/Netzwerk-Fehler | Automatischer Fallback auf LM Studio lokal |
| OpenCode Free-Rate-Limit | Retry mit exponentiellem Backoff (2s/4s/8s), danach Fallback |
| LM Studio nicht gestartet | LLM-Aufruf schlägt fehl, Prozess-Agent schreibt trotzdem Heartbeat (mit Fehler); Prozess-Worker nutzen dann Regel-Only-Logik (z.B. Research → reine Regel-Signale ohne LLM-Filter) |
| Execution 3 Order-Fehler in 5 Min | Killswitch → STOP_TRADING + Alarm an CEO/Board |
| Tages-DD > 3% | Killswitch (Risk Agent) |
| Wochen-DD > 7% | Killswitch + Board-Eskalation |
| Heartbeat-Ausfall > 2x Intervall | Watchdog alarmiert → CEO markiert Agent als "needs_restart" |
| RAM > 85% | Cost Optimizer empfiehlt Modell-Verzicht auf kostenlose Variante (kein lokales Laden) |
| Broker nicht erreichbar | Exponentieller Backoff, danach Alarm |

---

## 6. Deployment-Phasen

- **Phase 1 (Woche 1-2):** Alpaca Paper + BingX Testnet, `DRY_RUN=true`. Alle Agenten
  mit opencode-Free-Modellen. System einpendeln lassen.
- **Phase 2 (Woche 3-6):** Kleines Live-Kapital ($100 Alpaca + $50 BingX Spot).
  Execution Policy: CEO + Board Approval erforderlich für erste Live-Trades.
- **Phase 3 (ab Monat 2):** Skalierung erst wenn 60-Tage-Sharpe > 1.5 & DD < 8%.
  Bitunix als zweiter Crypto-Broker dazu.

---

## 7. Hardware-Auslastung nach Umstellung

Da die Free-Modelle in der Cloud laufen:
- **RAM:** ~2–3 GB (nur Python-Prozesse, Daten, kein geladenes LLM)
- **CPU:** Sehr gering (nur Indikatoren-Berechnungen, Pandas-Operationen)
- **Netzwerk:** Einige MB/Stunde (OHLCV-Daten + LLM-Calls)
- **LM Studio:** Muss **nicht** im Normalbetrieb laufen! Nur bei Offline-Nutzung vorladen.

Du kannst also parallel noch andere Programme auf dem N150 ausführen – anders
als im ursprünglichen lokalen Modell-Setup, wo permanent 5GB RAM für das LLM
reserviert waren.

Wenn du zwischen Phasen wechselst (z.B. unterwegs ohne Internet), lässt du LM
Studio mit dem 3B-Modell laufen – das System läuft ohne Unterbrechung weiter
(automatischer Fallback).
