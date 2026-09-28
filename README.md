# ZHF – Multi-Agent Algorithmic Trading System

> **Version 0.2.0 "FreeZen"** – siehe [CHANGELOG.md](CHANGELOG.md)

Ein vollständig lokales, **Paperclip-orchestriertes Multi-Agent-Handelssystem**
mit **kostenlosen OpenCode Zen-Free-Modellen** (keine API-Keys, keine Kosten).
> Entwickelt für passives Einkommen mit minimalem Kapitaleinsatz (ab ~500 €)
> auf **Alpaca** (Aktien/Krypto/Forex), **BingX** und **Bitunix** (Crypto-Perps).
> LM Studio dient als Offline-Fallback bei Netzausfall.

## Hardware (dein Setup)

- **CPU:** Intel N150 (4C/4T @ 3.6GHz) – reicht völlig aus, da alle
  intensiven LLM-Aufgaben in der Cloud (OpenCode-Free-Tier) laufen.
- **RAM:** 16 GB (nur ca. 2–3 GB durch Python-Pipelines belegt – KEIN
  permanenter LLM im Speicher!)
- **GPU:** nicht benötigt (iGPU unbenutzt)
- **Disk:** 20+ GB frei
- **OS:** CachyOS Linux (funktioniert auch auf jedem anderen Linux/macOS)

Im Gegensatz zum initialen lokalen Setup läuft das System jetzt **ohne
dauerhaft geladene LLMs** auf deinem Rechner – Ressourcenverbrauch ist minimal.

## Verwendete Free-Modelle (keine Anmeldung, keine Kosten)

| Agent | Free-Modell | Größe/Kontext | Aufgabe |
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
├── config/paperclip_agents.json# Fertige Agent-Konfiguration zum Import
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
├── scripts/
│   ├── common/         # config, logger, llm-Client (OpenCode+LMSTudio-Fallback), state
│   ├── research/       # Research-Agent (Prozess + opencode CLI)
│   ├── backtest/       # Backtest-Engine + Agent
│   ├── risk/           # Risk-Management
│   ├── execution/      # Order-Ausführung (KEIN LLM)
│   ├── cost/           # Cost-Optimizer
│   ├── ceo/            # Tagesbericht
│   ├── monitoring/     # Watchdog + Synth-Test
│   └── run_pipeline.py # Einmal komplett durchlaufen
├── strategies/         # Indikatoren, Signale, Watchlist, Strategie-Gewichte
├── .opencode/opencode.json  # OpenCode-Provider-Konfig (alle Free-Modelle)
├── .env.example        # API-Keys + Limits
└── requirements.txt
```

## Schnellstart

### 1. Setup-Skript ausführen
Das Skript prüft automatisch auf vorhandene Installationen (OpenCode, LM Studio,
Ollama, Paperclip, PostgreSQL, Docker) und nutzt diese wiederverwendbar:
```bash
cd /home/user/zhf
bash scripts/setup.sh
```
Ein detaillierter Report wird nach `data/setup_report.json` geschrieben.

### 2. Test der Free-Modelle (ohne Key!)
```bash
opencode run --model opencode/big-pickle --auto "Sag nur OK wenn du mich hörst."
```
Du solltest sofort eine Antwort von Big Pickle erhalten. (Wenn OpenCode noch nicht
installiert ist, gibt `scripts/setup.sh` dir den Installationsbefehl aus.)

### 3. Broker-Keys
```bash
cd /home/user/zhf
cp .env.example .env   # hat setup.sh bereits erledigt
nano .env              # Alpaca-Paper-Keys eintragen
```

### 4. Funktionstest (ohne echte Orders)
```bash
. .venv/bin/activate
python -m scripts.monitoring.synth_test   # Testdaten
python -m scripts.monitoring.watchdog     # Health-Check
```

### 5. Paperclip installieren
```bash
npx paperclipai onboard --yes
```
Dann gemäss `docs/PAPERCLIP_SETUP.md` die 6 Agenten + Watchdog im Dashboard
anlegen. Die Agent-Konfigurationen in `config/paperclip_agents.json` dienen
als Vorlage.

### 6. Pipeline laufen lassen
Sobald Alpaca-Paper-Keys gesetzt sind:
```bash
python -m scripts.run_pipeline
```
Die Heartbeats werden danach automatisch von Paperclip getriggert.

### 7. Tests und Konfig-Abgleich
```bash
python -m pip install -r requirements-dev.txt
python -m pytest              # 98 Tests, komplett offline (keine Broker-Requests)
python -m scripts.sync_env    # fehlende .env-Schlüssel aus .env.example ergänzen
python -m scripts.materialize_config   # Paperclip-Config auf den echten Repo-Pfad setzen
```

## Die 6 Agenten im Überblick

| Agent | Runtime | Free-Modell | Heartbeat | Aufgabe |
|-------|---------|-------------|-----------|---------|
| **CEO** Elara Voss | opencode_local | Nemotron 3 Ultra Free (550B) | Täglich EOD | Strategie, Berichte |
| **Research** Jordan Chen | opencode_local | Big Pickle | Alle 30 Min | Signale |
| **Backtest** Priya Patel | process | GPT-5 Nano (nur Review) | Alle 15 Min | Historische Validierung |
| **CRO** Marcus Okonkwo | process | MiniMax M2.5 Free | Alle 5 Min | Limits, Killswitch |
| **Execution** Sasha Kowalski | process | KEIN LLM | 1 Min (Marktzeit) | Orders, Fills |
| **Cost Opt** Naomi Bergström | process | MiMo V2.5 Flash Free | Stündlich | Gebühren/Ressourcen |

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
- **Keine Cloud-Kosten für LLMs:** Alle Agenten nutzen die kostenlose Zen-Tier
  von OpenCode. Die einzigen Kosten sind Handelsgebühren bei den Brokern.
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
