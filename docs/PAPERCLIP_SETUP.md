# Paperclip Setup – Schritt für Schritt (aktualisiert auf Free-Modelle)

> Auf deinem Intel N150 / CachyOS mit 16GB RAM.
> **Primär:** OpenCode Zen-Free-Modelle (Big Pickle, Nemotron Ultra, MiMo,
> MiniMax, LongCat, Space Bunny, GPT-5-Nano) – **keine API-Keys nötig,
> keine Kosten**.
> **Fallback:** Automatische Erkennung eines lokalen LLM-Servers (LM Studio
> auf :1234, Ollama auf :11434, llama.cpp auf :8080, vLLM auf :8000).

## 0. Automatische Erkennung

Zuerst das Setup-Skript ausführen – es erkennt bereits vorhandene Installationen
von OpenCode, LM Studio, Ollama, Paperclip, PostgreSQL und Docker und nutzt sie
wieder:

```bash
cd /home/user/zhf
bash scripts/setup.sh
```

Es schreibt einen Bericht nach `data/setup_report.json` und sagt dir genau, was
noch fehlt. Die Python-Umgebung wird automatisch angelegt.

> **Pfade:** Die Beispiele hier und `config/paperclip_agents.json` gehen von einem
> Checkout unter `/home/user/zhf` aus. Liegt dein Repo woanders, einmal
> `python -m scripts.materialize_config` laufen lassen – das schreibt
> `config/paperclip_agents.local.json` (und mit `--inplace --docs` auch die
> Beispielpfade in README/docs) mit deinem echten Pfad.

## 1. OpenCode CLI (wird von setup.sh geprüft)

Wenn `scripts/setup.sh` OpenCode als fehlend meldet:
```bash
npm install -g opencode-ai
# oder: curl -fsSL https://opencode.ai/install | bash
```
Prüfen:
```bash
opencode --version    # ≥ 1.18 erwartet
```

### 1.1 Konfiguration ins Projekt kopieren
Die `.opencode/opencode.json` im Repo enthält bereits alle Free-Modelle.
Sie wird automatisch von OpenCode beim Start gelesen.

Überprüfe die Modelle:
```bash
cd /home/user/zhf
opencode models opencode   # listet alle Free-Modelle aus Zen auf
```

### 1.2 Erster Funktionstest eines Free-Modells
```bash
cd /home/user/zhf
opencode run --model opencode/big-pickle --auto "Sag nur 'OK' wenn du mich hörst."
```
Du solltest eine Antwort von Big Pickle ohne API-Key sehen.

## 2. LM Studio (nur als Fallback, optional)

Wenn du Offline-Betrieb möchtest:

1. LM Studio Desktop installieren: https://lmstudio.ai
2. Modelle als GGUF Q4_K_M laden:
   - `Qwen/Qwen2.5-7B-Instruct-GGUF` (~4.7GB) – CEO Fallback
   - `meta-llama/Llama-3.2-3B-Instruct-GGUF` (~2.0GB) – Research/Risk Fallback
   - `Qwen/Qwen2.5-1.5B-Instruct-GGUF` (~1.0GB) – Cost Fallback
3. Local Server auf Port `1234` starten
4. **Wichtig:** LM Studio muss im Normalbetrieb **nicht** laufen! Die Free-Modelle
   laufen in der Cloud. Der Client fällt automatisch auf LM Studio zurück wenn
   OpenCode nicht erreichbar ist.

## 3. Paperclip installieren

```bash
# Node.js 22 ist bereits vorhanden
node --version   # muss ≥18 sein
npx paperclipai onboard --yes
```

Der Server läuft danach auf http://localhost:3100 – Dashboard öffnet sich automatisch.

## 4. Company "zhf-trading" anlegen

1. Im Dashboard → **New Company** → Name: `zhf-trading`, Timezone `Europe/Berlin`
2. Du als Board (einziger Mensch).

## 5. Agenten einstellen

Für JEDEN Agenten im Dashboard → **Hire Agent** und gemäss nachfolgender
Tabelle konfigurieren. Die System-Prompts sind bereits als `.md`-Dateien im
`prompts/`-Ordner vorbereitet.

### CEO – Elara Voss
- **Title:** Chief Executive Officer
- **Reports to:** Board
- **Adapter:** `opencode_local`
- **CWD:** `/home/user/zhf`
- **Model:** `opencode/nemotron-3-ultra-free`  ← das grosse 550B-Free-Modell
- **System Prompt:** Inhalt von `prompts/ceo.md` (kopieren oder `system_prompt_file` auf Pfad setzen)
- **CLI Flags:** `--auto` (erforderlich für nicht-interaktiven Modus)
- **Heartbeat:** `0 4 * * 2-6` (EOD nach NY-Close)
- **Budget:** 0 $ (Free-Tier)

### Head of Research – Jordan Chen
- **Title:** Head of Research
- **Reports to:** CEO
- **Adapter:** `opencode_local`
- **CWD:** `/home/user/zhf`
- **Model:** `opencode/big-pickle`
- **System Prompt:** `prompts/research_agent.md`
- **Heartbeat:** `*/30 * * * *`

### Head of Strategy Validation – Priya Patel
- **Title:** Head of Strategy Validation
- **Reports to:** CEO
- **Adapter:** `process`
- **Command:** `/home/user/zhf/.venv/bin/python -m scripts.backtest.run`
- **CWD:** `/home/user/zhf`
- **Heartbeat:** `*/15 * * * *`
- **Hinweis:** Der Backtest ist numerisch und ruft nur für die kurze
  Plausibilitäts-Review `opencode/gpt-5-nano` auf.

### CRO – Marcus Okonkwo
- **Title:** Chief Risk Officer
- **Reports to:** CEO
- **Adapter:** `process`
- **Command:** `/home/user/zhf/.venv/bin/python -m scripts.risk.run`
- **Heartbeat:** `*/5 * * * *` (für Crypto 24/7: `*/5 * * * *`; Aktien-only: `*/5 13-22 * * 1-5`)
- **Internes LLM:** `opencode/minimax-m2.5-free`

### Head of Trade Execution – Sasha Kowalski
- **Title:** Head of Trade Execution
- **Reports to:** CEO
- **Adapter:** `process`
- **Command:** `/home/user/zhf/.venv/bin/python -m scripts.execution.run`
- **Heartbeat:**
  - Aktien: `* 13-21 * * 1-5` (während US-Handelszeit)
  - Crypto 24/7: `* * * * *`
- **KEIN LLM!** Deterministisch.
- **Execution Policy:** "Requires CRO approval" (in Paperclip aktivieren)

### Head of Operational Efficiency – Naomi Bergström
- **Title:** Head of Operational Efficiency
- **Reports to:** CEO
- **Adapter:** `process`
- **Command:** `/home/user/zhf/.venv/bin/python -m scripts.cost.run`
- **Heartbeat:** `0 * * * *`
- **Internes LLM:** `opencode/mimo-v2.5-flash-free`

### Watchdog
- Leg als zusätzliche Routine unter dem CEO an:
- **Command:** `/home/user/zhf/.venv/bin/python -m scripts.monitoring.watchdog`
- **Cron:** `*/10 * * * *`

**Alternativ:** Importiere die vorbereitete Agentenliste aus
`config/paperclip_agents.json` per Paperclip-CLI, falls deine Version das
unterstützt.

## 6. Skills zuweisen

Die Skills liegen in `agents/<rolle>/skills/`. Im Dashboard unter
**Skills → Import** die Markdown-Dateien hochladen, oder sie direkt auf
Dateisystemebene in Paperclips Skills-Verzeichnis kopieren. Siehe
`docs/SKILLS.md` für Details.

## 7. API-Keys für Broker konfigurieren

```bash
cd /home/user/zhf
cp .env.example .env
nano .env
```

Für den **Start reicht ein Alpaca-Paper-Account** (kostenlos, keine
Brokerage-Einzahlung nötig):
1. Auf https://app.alpaca.markets/signup registrieren
2. Paper-Trading auswählen
3. API-Keys generieren
4. In `.env` eintragen (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`)

BingX und Bitunix erst später (in Phase 2/3).

## 8. Python-Umgebung vorbereiten

```bash
cd /home/user/zhf
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

(Die venv ist im Repo bereits vorhanden.)

## 9. Testlauf

`scripts/setup.sh` hat `.venv` und Dependencies bereits eingerichtet.

```bash
cd /home/user/zhf
. .venv/bin/activate

# 1. Synthetische Testdaten generieren (ohne API-Keys / ohne laufendes LLM)
python -m scripts.monitoring.synth_test

# 2. Health-Check
python -m scripts.monitoring.watchdog

# 3. Wenn Alpaca-Keys gesetzt und OpenCode erreichbar ist:
python -m scripts.run_pipeline
```

## 10. Heartbeats/Routinen in Paperclip aktivieren

Im Dashboard die Cron-Zeiten aus Schritt 5 aktivieren.

## 11. Monitoring

- **Dashboard:** http://localhost:3100
- **Täglicher Bericht:** `data/reports/daily_report.md`
- **System-Checks:** `data/reports/watchdog.json`
- **Logs:** `data/logs/*.log` (autorotierend bei 5MB)

## Troubleshooting

| Problem | Ursache | Fix |
|---------|---------|-----|
| `opencode run → unknown certificate verification error` | TLS/Netzwerk-Fehler (Firmen-Proxy, restriktives Netz) | Netzwerk prüfen; alternativ **OPENCODE_BIN=lms** oder LM-Studio-Fallback nutzen; das System fällt automatisch zurück |
| `model not found` | Modell-ID existiert nicht im Zen-Katalog | `opencode models opencode` ausführen und verfügbare Free-Modelle prüfen; ggf. Alias in `.opencode/opencode.json` anpassen |
| `free usage limit exceeded` | Free-Tier-Limit erreicht (selten) | Warte einige Minuten; oder weiche auf anderes Free-Modell aus (z.B. Big Pickle statt Nemotron) |
| Alle opencode-Aufrufe fallen auf LM Studio zurück | LM Studio ist der Offline-Fallback; es funktioniert nur wenn er läuft. | Wenn du KEINEN Offline-Betrieb willst: LM Studio stoppen; Fehlermeldungen werden im Log sichtbar |
| Execution platziert keine Orders | DRY_RUN=true oder Markt geschlossen | `DRY_RUN=false` erst nach ausgiebigem Paper-Testing setzen; Aktien nur während US-Handelszeit |
| Killswitch aktiv | Siehe `data/killswitch.json` für Reason | Ursache beheben (DD?, Verbindungsfehler?); dann `python -c "from scripts.common.state import SharedState; SharedState.deactivate_killswitch()"` |

## Modell-Übersichtstafel für den Fall von Ausfällen

Wenn eines der Free-Modelle mal nicht erreichbar sein sollte, kannst du im
`.opencode/opencode.json` oder per `--model`-Flag auf ein anderes wechseln:

| Aufgabe | Bevorzugt | Alternative 1 | Alternative 2 | Fallback (lokal) |
|---------|-----------|---------------|---------------|------------------|
| CEO / Tagesbericht | `nemotron-3-ultra-free` | `longcat-2.5-preview-free` | `nemotron-3-super-free` | `lmstudio/qwen2.5-7b` |
| Research | `big-pickle` | `mimo-v2-pro-free` | `minimax-m2.5-free` | `lmstudio/llama-3.2-3b` |
| Risk | `minimax-m2.5-free` | `big-pickle` | `nemotron-3-super-free` | `lmstudio/llama-3.2-3b` |
| Backtest-Review | `gpt-5-nano` | `mimo-v2.5-flash-free` | – | `lmstudio/qwen2.5-1.5b` |
| Cost | `mimo-v2.5-flash-free` | `gpt-5-nano` | `space-bunny-free` | `lmstudio/qwen2.5-1.5b` |
| Privacy-sensitive Analysen | `space-bunny-free` | – | – | lokales Modell |
