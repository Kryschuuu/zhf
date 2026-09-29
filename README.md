# ZHF – Multi-Agent Algorithmic Trading System

> **Version 0.3.0 "SafeBootstrap"** – siehe [CHANGELOG.md](CHANGELOG.md)

Ein lokal betriebenes, **Paperclip-orchestriertes Multi-Agent-Handelssystem**
mit OpenCode Zen als bevorzugtem LLM-Harness und lokalem LM-Studio/Ollama-Fallback.
Modellverfügbarkeit wird beim Setup geprüft; fehlende Zen-Modelle werden durch
verfügbare Alternativen ersetzt.
> Entwickelt für passives Einkommen mit minimalem Kapitaleinsatz (ab ~500 €)
> auf **Alpaca** (Aktien/Krypto/Forex), **BingX** und **Bitunix** (Crypto-Perps).
> LM Studio dient als Offline-Fallback bei Netzausfall.

## Hardware (dein Setup)

- **CPU:** Intel N150 (4C/4T @ 3.6GHz) – für Python-Pipelines und
  regelbasierte Fallbacks; Remote-LLM-Aufgaben hängen von Provider-Verfügbarkeit ab.
- **RAM:** 16 GB (nur ca. 2–3 GB durch Python-Pipelines belegt – KEIN
  permanenter LLM im Speicher!)
- **GPU:** nicht benötigt (iGPU unbenutzt)
- **Disk:** 20+ GB frei
- **OS:** CachyOS Linux (funktioniert auch auf jedem anderen Linux/macOS)

Im Gegensatz zum initialen lokalen Setup läuft das System jetzt **ohne
dauerhaft geladene LLMs** auf deinem Rechner – Ressourcenverbrauch ist minimal.

## Bevorzugte OpenCode-Modelle und Fallbacks

Die Tabelle zeigt Präferenzen, keine feste Verfügbarkeitsgarantie: Setup und
LLM-Client prüfen `opencode models opencode` und wählen pro Agent ein verfügbares
Ersatzmodell. Zen-Zugang, Rate-Limits und Nutzungsbedingungen hängen vom aktuellen
OpenCode-Angebot ab.

| Agent | Bevorzugtes Modell | Größe/Kontext | Aufgabe |
|-------|-------------|---------------|---------|
| **CEO** | `opencode/nemotron-3-ultra-free` (Neotone 3 Ultra Free / NVIDIA Nemotron 3 Ultra 550B) | 204k ctx | Tagesberichte, Strategie-Entscheidungen |
| **Research** | `opencode/big-pickle` | 200k ctx | Marktscan, Signale |
| **CRO** | `opencode/minimax-m2.5-free` | 200k ctx | Risk-Checks, Positionsgrössen |
| **Cost** | `opencode/mimo-v2.5-flash-free` (Xiaomi) | 16k ctx | Gebühren/Slippage/Ressourcen |
| **Backtest-Review** | `opencode/gpt-5-nano` | 8k ctx | Kurze Plausibilitäts-Kommentare |
| **Execution** | – (KEIN LLM) | – | Rein deterministisch |
| **Backtest-Engine** | – (KEIN LLM) | – | Rein numerisch (pandas/numpy) |

**Backup/Reserve:**
- `opencode/longcat-2.5-preview-free` (lange Aufgaben)
- `opencode/nemotron-3-super-free` (120B)
- `opencode/nemotron-3.5-lightning-free` (schnell)
- `opencode/space-bunny-free` (zero-retention/privacy)
- `opencode/mimo-v2-pro-free` (Xiaomi Coding)

**Offline-Fallback via LM Studio:** Qwen 2.5 7B / Llama 3.2 3B / Qwen 2.5 1.5B
(alle Q4_K_M, CPU-only, werden automatisch verwendet wenn OpenCode nicht erreichbar ist).

## Verzeichnisstruktur

```
zhf/
├── agents/                     # Paperclip-Skills pro Agent (Markdown)
├── config/paperclip_agents.json# Versionierte Paperclip-/Agenten-Blaupause
├── data/                       # Runtime-State (vom System beschrieben)
│   ├── signals/      candidates.json → validated.json
│   ├── orders/       approved.json, state.json
│   ├── heartbeats/   *.json pro Agent
│   ├── reports/      daily_report.md, fee_report.json, watchdog.json
│   └── logs/         fills.log + Agent-Logs
├── docs/
│   ├── ARCHITECTURE.md         # Architektur, Harness-Wahl, Modell-Zuordnung
│   ├── PAPERCLIP_SETUP.md      # Schritt-für-Schritt Setup
│   ├── AGENT_SKILLS_MATRIX.md  # Input/Output/Interaktion pro Agent
│   ├── CEO_TASKS.md            # P0–P4 CEO-Aufgaben
│   └── SKILLS.md               # Skills-Überblick
├── exchanges/          # Broker-Adapter (Alpaca, BingX, Bitunix)
├── prompts/            # System-Prompts (CEO, Research, Risk, Exec, Cost, Backtest)
├── setup-script.sh    # Bash-Setup; Fish-Start über setup-script.fish
├── setup-script.fish  # Fish-kompatibler Launcher
├── Dockerfile.paperclip / docker-compose.paperclip.yml # persistentes Paperclip-Docker-Setup
├── scripts/
│   ├── common/         # config, logger, LLM-Client, Modell-Fallbacks, State
│   ├── research/       # Research-Agent (Prozess + opencode CLI)
│   ├── backtest/       # Backtest-Engine + Agent
│   ├── risk/           # Risk-Management
│   ├── execution/      # Order-Ausführung (KEIN LLM)
│   ├── cost/           # Cost-Optimizer
│   ├── ceo/            # Tagesbericht
│   ├── monitoring/     # Watchdog + Synth-Test
│   ├── paperclip_provision.py # Idempotente ZHF-Firmen-Provisionierung
│   └── run_pipeline.py # Einmal komplett durchlaufen
├── strategies/         # Indikatoren, Signale, Watchlist, Strategie-Gewichte
├── .opencode/opencode.json  # OpenCode-Provider-Konfig (alle Free-Modelle)
├── .env.example        # API-Keys + Limits
└── requirements.txt
```

## Schnellstart

### 1. Setup ausführen

Das idempotente Setup prüft Python, Node/Paperclip, Docker, OpenCode-Konfiguration
und den Zen-Modellkatalog. Es erstellt `.venv` und `.env` (ohne bestehende Werte
zu überschreiben), zeigt bei erkanntem Docker ein interaktives Menü und startet
standardmäßig einen isolierten synthetischen Ende-zu-Ende-Test.

```bash
./setup-script.sh
# alternativ aus Fish:
fish setup-script.fish
# Rückwärtskompatibler Einstieg:
bash scripts/setup.sh
```

Der Synth-Test nutzt `data/synth`, `ZHF_SYNTH=1`, `ZHF_SKIP_LLM=1` und
`DRY_RUN=true`. Er spricht keine Broker-API an und kann keine echten Orders
platzieren. Der Diagnosebericht steht in `data/setup_report.json`.

Nützliche Optionen:

```bash
./setup-script.sh --check-only                 # nur Voraussetzungen prüfen
./setup-script.sh --mode docker                # Paperclip über Docker Compose starten
./setup-script.sh --mode native               # native Paperclip-Installation starten
./setup-script.sh --mode docker --provision-paperclip
./setup-script.sh --mode docker --invite-user --invite-role operator
```

Für Docker wird Paperclip auf `127.0.0.1` gebunden und im authentifizierten Modus
betrieben. Beim ersten Start den ersten Benutzer/Board-Owner im Browser anlegen,
CLI im Container koppeln und danach `--provision-paperclip` erneut ausführen.
Neue Benutzer werden über kurzlebige Paperclip-Einladungslinks registriert; das
Skript verschickt keine E-Mails.

### 2. Broker-Schlüssel und Modelle

```bash
nano .env                 # zum Start nur Alpaca-Paper-Keys ergänzen
python -m scripts.common.models
```

Die bevorzugten Zen-Modelle sind keine feste Laufzeitannahme: Das Setup wählt pro
Agent das erste verfügbare Modell aus der versionierten Fallback-Reihenfolge.
Wenn OpenCode oder der Katalog nicht erreichbar sind, wird ein lokaler
OpenAI-kompatibler Server (LM Studio/Ollama/llama.cpp/vLLM) verwendet, sofern er
läuft und aus der ausführenden Laufzeit erreichbar ist; andernfalls greifen
regelbasierte bzw. deterministische Pfade. In Docker ist `127.0.0.1` der
Container, nicht der Host; ein Host-LLM muss bewusst über eine erreichbare
Gateway-Adresse konfiguriert werden.

### 3. Paperclip-Firma provisionieren

Nach der Ersteinrichtung/CLI-Anmeldung:

```bash
./setup-script.sh --mode docker --provision-paperclip
# oder direkt:
python -m scripts.paperclip_provision --action provision
```

Die Provisionierung legt `zhf-trading`, sieben Agenten, die Skills aus `agents/`
und vier unzugewiesene Setup-Aufgaben an. Sie ist wiederholbar: bereits vorhandene
Objekte werden wiederverwendet. Neue Agenten bleiben im Leerlauf, ihre Heartbeats
sind deaktiviert, Aufgaben starten keine Agenten. Neue menschliche Mitglieder
können mit `--invite-user --invite-role operator` eingeladen werden.

### 4. Offline-Funktionstest und Monitoring

```bash
. .venv/bin/activate
python -m scripts.monitoring.synth_test --keep
python -m scripts.monitoring.watchdog
python -m pytest
```

### 5. Operative Pipeline

Paperclip-Agenten/Heartbeats und echte Broker-Ausführung werden **nicht** durch
das Setup aktiviert. Erst nach Prüfung der synthetischen Reports, Risikolimits,
Paper-Trading-Zugangsdaten und Broker-Policies dürfen Heartbeats manuell aktiviert
werden. `python -m scripts.run_pipeline` ist ein regulärer Broker-Pipeline-Lauf;
für Tests ohne Broker verwende `python -m scripts.run_pipeline --synth --no-llm`.

### 6. Tests und Konfig-Abgleich

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m scripts.sync_env
python -m scripts.materialize_config
```

## Die 6 Agenten im Überblick

| Agent | Runtime | bevorzugtes Modell | Heartbeat (manuell zu aktivieren) | Aufgabe |
|-------|---------|-------------|-----------|---------|
| **CEO** Elara Voss | opencode_local | Nemotron 3 Ultra Free (550B) | Täglich EOD | Strategie, Berichte |
| **Research** Jordan Chen | opencode_local | Big Pickle | Alle 30 Min | Signale |
| **Backtest** Priya Patel | process | GPT-5 Nano (nur Review) | Alle 15 Min | Historische Validierung |
| **CRO** Marcus Okonkwo | process | MiniMax M2.5 Free | Alle 5 Min | Limits, Killswitch |
| **Execution** Sasha Kowalski | process | KEIN LLM | 1 Min (Marktzeit) | Orders, Fills |
| **Cost Opt** Naomi Bergström | process | MiMo V2.5 Flash Free | Stündlich | Gebühren/Ressourcen |

Der Watchdog ist ein zusätzlicher siebter Paperclip-Agent. Die automatisierte
Provisionierung erstellt alle sieben Agents mit deaktivierten Heartbeats; die
Zeitangaben oben sind empfohlene spätere Betriebsintervalle, keine aktiven Jobs.

## Handelsstrategien (capital-effizient für kleine Konten)

1. **Mean Reversion** – RSI < 30 long, > 70 short
2. **Bollinger-Band Breakout** (Volume ≥ 1.3x bestätigt)
3. **EMA-Crossover** (EMA9/EMA21)
4. **MACD-Flip**
5. **Funding-Arbitrage-Hinweise** (nur Crypto-Perps)

Alle Signale passieren einen harten Backtest-Filter:
min 15 Trades, Winrate ≥ 45%, PF ≥ 1.3, Sharpe ≥ 1.0, Max-DD ≤ 15%.

## Risikolimits (hard)

- Max 2% Verlust pro Position
- Max 8 offene Positionen
- Max 3x Hebel bei Crypto-Perps
- Max 3% Tages-Drawdown → **KILLSWITCH**
- Max 7% Wochen-Drawdown → **KILLSWITCH**
- R:R ≥ 1:2, Stop-Loss Pflicht
- Korrelation ≤ 0.7 zwischen Positionen

## Deployment-Phasen

| Phase | Dauer | Konto | Kapital | Ziel |
|-------|-------|-------|---------|------|
| **Phase 1** | Woche 1-2 | Alpaca Paper + BingX Testnet | $100k Papiergeld | Einfahren, Fehlerbehandlung, Metriken validieren |
| **Phase 2** | Woche 3-6 | Alpaca Live ($100) + BingX Spot ($50) | ~150 € | Kleinstkapital, echte Gebühren/Slippage messen |
| **Phase 3** | ab Monat 2 | Alle 3 Broker live | schrittweise | Skalierung NUR bei 60-Tage-Sharpe > 1.5 & DD < 8% |

## Fehlersuche

Kurzreferenz – ausführlich in **[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)**
(Befund → Ursache → Fix → Prüfbefehl).

| Befund im Log | Ursache | Fix |
|---|---|---|
| `subscription does not permit querying recent SIP data` | Free-Plan darf kein SIP für Daten < 15 min | `ALPACA_DATA_FEED=iex` (Default `auto` = iex), Uhrzeit prüfen |
| DNS-Fehler auf `fapi-sim.bitunix.com` | Bitunix hat **kein** Futures-REST-Testnet | `BITUNIX_BASE_URL=https://fapi.bitunix.com`; `BITUNIX_TESTNET=true` ⇒ read-only, Live-Orders nur mit `BITUNIX_ALLOW_LIVE_ORDERS=true` |
| `SYNTH_* … invalid symbol` bei Alpaca | alter Selbsttest schrieb Testdaten in `data/signals` | Selbsttest läuft isoliert in `data/synth` (`ZHF_DATA_DIR`) |
| Risk `0/3 approved`, „no data“ | Datenlage und „keine Chance“ waren nicht unterscheidbar | `data/market_data/status.json` (ok/empty/error/stale), Watchdog meldet `critical` |
| Orders hängen 300 s / Fill-Preis 0 | Bracket-Beine wurden in offenen Orders gesucht | `get_order(order_id)`, Dry-Run-Fill mit Referenzpreis |
| Limit-Order unter Kosten | Take-Profit < Round-Trip-Fee | `scripts/common/fees.py` + Risk-Gate `min_sane_take_profit_pct` |

Wo nachsehen: `data/reports/watchdog.json`, `data/market_data/status.json`,
`data/signals/rejections.json`, `data/orders/recent_errors.json`, `data/logs/fills.log`.
Killswitch zurücksetzen: `python -m scripts.monitoring.watchdog --reset-killswitch`.

## Monitoring

- **Paperclip Dashboard:** http://localhost:3100
- **Täglicher CEO-Bericht:** `data/reports/daily_report.md`
- **System-Checks:** `data/reports/watchdog.json` (alle 10 Min)
- **Logs:** `data/logs/*.log` (autorotierend)
- **Fills/Gebühren:** `data/logs/fills.log`, `data/reports/fee_report.json`
- **Broker-Gesundheit:** `python -m scripts.monitoring.watchdog` → Breaker, Cooldowns

## Wichtige Hinweise

- **Kein finanzieller Rat:** Trading birgt Verlustrisiken. Nutze nur Kapital,
  das du entbehren kannst. Teste ausführlich im Paper-Modus, bevor du live
  gehst.
- **Modellnutzung:** Zen-Tier und Free-Modelle können ohne eigene Provider-Keys
  verfügbar sein; Verfügbarkeit, Limits und Nutzungsbedingungen ändern sich.
  Es werden keine bezahlten Provider-Keys durch das Setup angelegt.
- **Offline-fähig:** Wenn Internet ausfällt, fällt der LLM-Client automatisch
  auf das lokale LM Studio zurück (sofern ein Modell geladen ist).
- **Determinismus bei Orders:** Execution und Backtest laufen OHNE LLM,
  sodass keine Halluzinationen dein Kapital gefährden können.

## Dokumentation

- **`docs/ARCHITECTURE.md`** – Detaillierte Architektur, Harness-Wahl, Modell-Begründung
- **`docs/PAPERCLIP_SETUP.md`** – Setup-Anleitung (OpenCode → Paperclip → Agenten)
- **`docs/AGENT_SKILLS_MATRIX.md`** – Input/Output/Interaktion je Agent
- **`docs/TROUBLESHOOTING.md`** – Fehlersuche: Befund → Ursache → Fix → Prüfbefehl
- **`docs/CEO_TASKS.md`** – P0–P4 Aufgabenkatalog für den CEO
- **`docs/SKILLS.md`** – Skills-Überblick

## Lizenz

MIT (siehe `LICENSE`). Nur zu Bildungs-/Forschungszwecken – kein finanzieller Rat.
