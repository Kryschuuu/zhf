# Changelog

Alle bemerkenswerten Änderungen am ZHF Multi-Agent Trading System werden in
dieser Datei dokumentiert. Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.0.0/)
und die Versionierung folgt [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] – FreeZen – 2026-09-28

### Hinzugefügt
- **OpenCode Zen-Free-Tier als primärer Harness:** Alle LLM-Agenten nutzen
  jetzt die kostenlosen OpenCode-Modelle (keine API-Keys, keine Kosten):
  - CEO → `nemotron-3-ultra-free` (NVIDIA Neotone/Nemotron 3 Ultra, 550B MoE, 204k ctx)
  - Research → `big-pickle`
  - Risk/CRO → `minimax-m2.5-free`
  - Cost Optimizer → `mimo-v2.5-flash-free` (Xiaomi)
  - Backtest-Review → `gpt-5-nano`
  - Backup-Optionen: `longcat-2.5-preview-free`, `nemotron-3-super-free`,
    `nemotron-3.5-lightning-free`, `space-bunny-free`, `mimo-v2-pro-free`
- **Automatischer Fallback:** LLM-Client fällt bei TLS-/Netzwerk-/Timeout-Fehlern
  transparent auf ein lokales LM-Studio-Modell zurück (Offline-Betrieb möglich).
- **Ollama-Support:** LM-Studio-kompatible lokale Endpunkte (Port 11434) werden
  als weitere lokale Fallback-Option erkannt (über `.env` `LOCAL_LLM_BASE_URL`
  konfigurierbar).
- **Erweitertes Setup-Skript** (`scripts/setup.sh`): Erkennt automatisch
  vorhandene Installationen von Ollama, LM Studio, Paperclip, PostgreSQL, Docker
  und Docker-Container und verwendet diese, anstatt sie neu zu installieren.
- **Watchdog** prüft jetzt OpenCode-CLI, LM-Studio-Verfügbarkeit und zeigt beide
  Status im Report.
- **Versionierung:** `scripts/common/version.py` und dieses CHANGELOG.
- **Bitunix-Adapter** mit eigener HMAC-Signierung (nicht in CCXT vorhanden).
- **BingX-Adapter** über CCXT mit korrekter Sandbox/defaultType-Optionen.

### Geändert
- **Harness-Architektur:** CEO und Research laufen jetzt über den Paperclip-
  `opencode_local`-Adapter mit explizitem Free-Modell statt direkt LM Studio.
  Risk, Cost und Backtest-Review laufen als `process`-Agenten, die intern
  die OpenCode CLI aufrufen.
- **Dokumentation** überarbeitet:
  - `docs/ARCHITECTURE.md` – neue Harness-Topologie (Cloud-Primär/Lokal-Fallback)
  - `docs/PAPERCLIP_SETUP.md` – Setup mit Free-Modellen
  - `docs/AGENT_SKILLS_MATRIX.md` – Modell-Zuordnung
  - `README.md` – überarbeitet, Modell-Tabelle
  - `docs/CEO_TASKS.md` – keine inhaltlichen Änderungen
- **Modell-Auswahl** pro Agent wird zentral in `DEFAULT_AGENT_MODEL` in
  `scripts/common/llm.py` und in `.opencode/opencode.json` gehalten.
- `.env.example` um OpenCode-Konfiguration ergänzt (OPENCODE_BIN, _TIMEOUT,
  _DISABLE_TELEMETRY, LOCAL_LLM_BASE_URL, LOCAL_LLM_PROVIDER_NAME).
- `.opencode/opencode.json` mit allen Free-Modellen und dem LM-Studio-Fallback.
- `config/paperclip_agents.json` mit den neuen Modellen und Environment-Variablen.

### Behoben
- **Sys-Path** in allen Skripten auf `parent.parent.parent` konsistent
  gehalten (für den Aufruf aus dem Repo-Root als `python -m scripts.X.run`).
- **ccxt_exchange:** BingX defaultType (`swap`) und sandboxMode werden jetzt
  korrekt an ccxt übergeben.
- **LLM-JSON-Parser:** robusteres Extrahieren des JSON-Objekts (Regex auf
  erste `{...}`-Gruppe), Streicht ANSI-Escapes und ``` ```-Blöcke.
- **cost_optimizer/call_llm:** `agent="cost"` wird jetzt korrekt übergeben
  (statt des lokalen LM-Studio-Modellnamens).
- **backtest/call_llm:** `agent="backtest"` verwendet jetzt `gpt-5-nano`.
- **ceo/call_llm:** `agent="ceo"` verwendet jetzt Nemotron-3-Ultra-Free
  statt lokalem 7B-Modell.
- **Killswitch-Reset** wird korrekt aus dem state geladen
  (Deaktivierung nach Ursachenbeseitigung möglich).
- **Watchdog** gibt nicht mehr fälschlicherweise Alarm, wenn LM Studio nicht
  läuft (LM Studio ist nur noch Fallback, nicht erforderlich).

### Entfernt
- Abhängigkeit von einem permanent laufenden lokalen LLM entfernt. LM Studio
  muss im Normalbetrieb nicht mehr gestartet sein und wird nur bei Bedarf
  automatisch angefragt.

### Bekannte Einschränkungen
- OpenCode Free-Tier kann Rate-Limits haben; bei wiederholtem Fehler fällt
  das System auf das lokale LLM zurück (wenn LM Studio läuft).
- TLS zu opencode.ai kann hinter restriktiven Firewalls/Proxies scheitern –
  dann LM-Studio-Fallback verwenden.
- Bitunix-API-Endpunkte sind anhand der öffentlichen Dokumentation
  implementiert und müssen mit echten API-Keys verifiziert werden.

## [0.1.0] – Initial – 2026-09-28

### Hinzugefügt
- Grundgerüst des Multi-Agent-Handelssystems.
- Sechs Agenten: CEO, Research, Backtest, Risk, Execution, Cost Optimizer.
- Alpaca-Adapter (Stocks/Crypto via alpaca-py).
- BingX-Adapter via CCXT.
- Einfacher Backtester (pandas/numpy, keine externen Frameworks).
- Indikatoren und Signal-Generator (RSI, MACD, Bollinger, EMA, ATR).
- Killswitch und Circuit-Breaker.
- Watchdog (Heartbeat-, Broker-, Ressourcen-Monitoring).
- Atomares JSON-Datei-Shared-State.
- Papierclip-Konfiguration.
- System-Prompts für alle Agenten.
- Synthetischer Daten-Generator für Offline-Pipeline-Tests.
- Deployment in 3 Phasen (Paper → Kleinstkapital → Skalierung).
