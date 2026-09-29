# Multi-Agent Algorithm Trading System (MATS)
## Orchestriert via Paperclip · OpenCode Zen (bevorzugt) · OpenAI-kompatible lokale LLMs (Fallback)

> **Ziel:** Forschungs- und Paper-Trading-Pipeline über Aktien (Alpaca), Krypto-Spot
> und Perpetuals (BingX, Bitunix) mit expliziten Risiko- und Freigabegates.
>
> **Modellrouting:** Zen-Modell-IDs sind zur Laufzeit veränderlich. `scripts/common/models.py`
> prüft den erreichbaren Katalog und wählt je LLM-Agent die erste verfügbare
> Präferenz/Fallback-ID. Wenn OpenCode nicht erreichbar ist, versucht der LLM-Client
> einen geladenen lokalen OpenAI-kompatiblen Server (LM Studio, Ollama, llama.cpp,
> vLLM). Verfügbarkeit, Anmeldung, Limits und Preisbedingungen liegen beim Provider.

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

### Modellzuordnung – Agent → bevorzugte OpenCode-ID

Die nachfolgenden IDs sind Präferenzen. Das Setup und die Laufzeit prüfen die
Katalogverfügbarkeit und wählen anhand der geordneten Fallback-Liste aus
`scripts/common/models.py` ein verfügbares Zen-Modell.

| Agent | bevorzugte ID | Größe/Kontext laut Modellkonfiguration | Begründung | lokaler Fallback (falls geladen) |
|-------|----------------------|----------------|------------|-------------------------------|
| **CEO** | `opencode/nemotron-3-ultra-free` | providerabhängig | Präferenz für Berichte/Reasoning; wird nur gewählt, wenn der Katalog die ID anbietet | beliebiges geladenes lokales Modell |
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

### Warum OpenCode als bevorzugter Harness?
1. **Kein permanent geladenes Modell nötig:** Lokale CPU/RAM-Ressourcen bleiben für
   Python, Daten und Monitoring frei.
2. **Modellwahl ist austauschbar:** Die Zen-Katalogprüfung und Fallback-Reihenfolge
   lassen sich unabhängig von den deterministischen Handelskomponenten aktualisieren.
3. **Provider-Konditionen sind nicht garantiert:** Zugang, Anmeldung, Rate-Limits und
   Kosten hängen vom aktuellen OpenCode-Angebot und der Betreiberkonfiguration ab.
4. **Privacy-Option:** Ein lokaler Endpunkt kann verwendet werden, wenn keine Daten an
   einen Remote-Provider gesendet werden sollen.
5. **Resilienz:** Bei Netzwerk-, Rate-Limit-, Timeout- oder Modellfehlern versucht der
   LLM-Client automatisch einen erreichbaren lokalen OpenAI-kompatiblen Server.

### Wann lokale LLMs verwendet werden
- Offline-Betrieb oder restriktive Firmen-Netze
- Wenn Remote-Modell/Katalog nicht erreichbar ist
- Wenn Datenschutzrichtlinien lokale Verarbeitung verlangen
- Wenn ein lokaler Endpunkt für bestimmte einfache Aufgaben bevorzugt wird

---

## 2. Harness-Auswahl & Konfiguration

### Harness-Architektur
| Schicht | Technologie | Begründung |
|---------|-------------|------------|
| **Orchestrierung** | Paperclip (Docker Compose oder native CLI ≥24.11.0) | Org-Chart, Tasks, Budgets, Routinen, Approvals, Audit-Dashboard |
| **LLM-Agenten (CEO, Research)** | `opencode_local`-Adapter | Paperclip startet OpenCode im Workspace; Modell-ID wird aus dem erreichbaren Katalog gewählt |
| **LLM-Aufrufe in Worker-Prozessen** | `scripts/common/llm.py` | OpenCode CLI mit geordneter Modell-Fallback-Liste; lokaler HTTP-Fallback bei Aufruffehlern |
| **Deterministische Worker (Execution, Backtest-Engine)** | `process`-Adapter | Python-Prozesse; Execution und Backtest-Engine verwenden kein LLM |
| **Lokaler Fallback** | LM Studio, Ollama, llama.cpp oder vLLM | Erreichbarer OpenAI-kompatibler `/v1/models`-Endpunkt; Modell muss geladen sein |

### OpenCode-Konfiguration (`$ZHF_REPO_ROOT/.opencode/opencode.json`)
Die zentrale Konfiguration definiert den Zen-Provider und optionale lokale Provider.
Die Laufzeit prüft die Zen-Modellliste; `.opencode/opencode.json` dokumentiert die
verfügbaren Präferenzen, ist aber keine Verfügbarkeitsgarantie.

### Adapter-Typen und Runtime-Pfade

| Agentengruppe | Adapter | Runtime-Konfiguration |
|---|---|---|
| CEO, Research | `opencode_local` | Modell aus der verfügbaren Zen-Fallback-Reihenfolge; Prompt-Datei aus `prompts/`; CWD = Checkout-Root |
| Risk, Backtest, Cost | `process` | `python -m scripts.<agent>.run`; LLM-Aufrufe laufen kontrolliert über `scripts/common/llm.py` |
| Execution, Watchdog | `process` | Deterministische Python-Worker; keine modellbasierte Orderentscheidung |

Native Installation verwendet den echten Checkout-Root und `.venv/bin/python`.
Im Docker-Modus zeigt Paperclip auf `/workspace/zhf` und
`/opt/zhf-venv/bin/python`; die ZHF-Dependencies sind im Runtime-Image installiert.

Die Provisionierung ist idempotent und überschreibt keine bestehenden Agents.
Neue Agents werden mit `runtimeConfig.heartbeat.enabled=false` erstellt. Routines
und geplante Heartbeats müssen nach Review im Paperclip-Dashboard aktiviert werden.

---

## 3. Datenfluss & Heartbeat-Rhythmen

Die Frequenzen im Diagramm sind Zielwerte für einen späteren Betrieb. Das Setup
legt Agents/Tasks an, startet aber keine Paperclip-Heartbeats oder Routinen. Ein
Betreiber prüft erst Synth-Report, Keys, Limits und Freigaben und aktiviert dann
gezielt die gewünschten Schedules.

```
Heartbeat-Schedule (nach manueller Freigabe)
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
| OpenCode-Rate-Limit oder Modellfehler | Retry mit exponentiellem Backoff (2s/4s/8s), danach lokaler Fallback sofern verfügbar |
| LM Studio nicht gestartet | LLM-Aufruf schlägt fehl, Prozess-Agent schreibt trotzdem Heartbeat (mit Fehler); Prozess-Worker nutzen dann Regel-Only-Logik (z.B. Research → reine Regel-Signale ohne LLM-Filter) |
| Execution 3 Order-Fehler in 5 Min | Killswitch → STOP_TRADING + Alarm an CEO/Board |
| Tages-DD > 3% | Killswitch (Risk Agent) |
| Wochen-DD > 7% | Killswitch + Board-Eskalation |
| Heartbeat-Ausfall > 2x Intervall | Watchdog alarmiert → CEO markiert Agent als "needs_restart" |
| RAM > 85% | Cost Optimizer empfiehlt Modell-Verzicht auf kostenlose Variante (kein lokales Laden) |
| Broker nicht erreichbar | Exponentieller Backoff, danach Alarm |

---

## 6. Deployment und Setup-Sicherheit

`setup-script.sh` bietet drei Paperclip-Wege: laufende Instanz wiederverwenden,
Paperclip in Docker Compose starten oder die native CLI verwenden. Docker bindet
standardmäßig auf `127.0.0.1`, startet im authentifizierten Modus und persistiert
Daten. Für die ZHF-Worker enthält das Docker-Image zusätzlich eine isolierte
Python-Umgebung; der Checkout wird nach `/workspace/zhf` gemountet.

Nach einem frischen Docker-Start legt ein Mensch im Browser den ersten Benutzer
und Board-Owner an und koppelt danach die CLI. `scripts.paperclip_provision`
verwendet die Paperclip-CLI und validiert Firma, sieben Agents, Skills und Tasks.
Bestehende Objekte werden wiederverwendet, nicht gelöscht oder still überschrieben.

Neu provisionierte Agents haben deaktivierte Heartbeats; Setup-Aufgaben sind
unzugewiesen und im Backlog. Die Schedule-Zeiten oben sind Zielkonfigurationen,
keine automatisch gestarteten Paperclip-Routinen. Der Setup-Lauf endet mit einem
isolierten Synth-Test (`data/synth`) und greift nicht auf echte Broker zu.

Empfohlene Aktivierung:

1. Synth- und Watchdog-Reports prüfen.
2. Paper-Trading-Schlüssel, `DRY_RUN=true`, Broker-Feeds und Risk-Gates prüfen.
3. Erst dann gewünschte Agent-Heartbeats/Routinen im Dashboard aktivieren.
4. Live-Trading bleibt eine separate, explizite Board-Entscheidung und darf nicht
   aus einer Setup- oder Modellprüfung abgeleitet werden.

## 7. Hardware-Auslastung

Wenn LLM-Aufgaben von einem erreichbaren Remote-Provider bedient werden, benötigt
der Host kein permanent geladenes Modell. Bei lokaler Verarbeitung hängt die
Auslastung vom geladenen Modell, Quantisierung und Laufzeit ab:

- **RAM/CPU:** Python-Pipeline ohne lokales LLM ist leichtgewichtig; lokale GGUF-
  Modelle benötigen zusätzlichen RAM/CPU.
- **Netzwerk:** Broker-Marktdaten plus modellabhängige Requests an den gewählten
  LLM-Provider.
- **LM Studio/Ollama:** Muss im Normalbetrieb nicht laufen, ist aber notwendig,
  wenn OpenCode ausfällt und ein lokaler Fallback gewünscht ist.

Die automatische Fallback-Erkennung setzt einen kompatiblen, laufenden
`/v1/models`-Endpunkt voraus. Ohne erreichbaren Remote- oder lokalen LLM bleiben
sichere regelbasierte bzw. deterministische Fallbacks maßgeblich.
