# Paperclip Setup für ZHF 0.3.0

Diese Anleitung beschreibt das aktuelle, automatisierte Setup. Paperclip ist der
Control-Plane für ZHF-Agenten; OpenCode und die Python-Worker sind die
Ausführungs-Runtimes.

## Sicherheitsmodell

- Setup erstellt Agenten mit deaktivierten Heartbeats. Die vier initialen Aufgaben
  sind unzugewiesen und liegen im Backlog.
- Der automatisch ausgeführte Pipeline-Test ist **synthetisch und isoliert**:
  `ZHF_SYNTH=1`, `ZHF_SKIP_LLM=1`, `DRY_RUN=true`, `data/synth/`.
- Es werden keine Broker-APIs angesprochen und keine echten Orders erzeugt.
- Live-Trading, Agenten-Heartbeats und menschliche Zugänge werden nicht ohne
  bewusste Betreiberaktion aktiviert.
- Paperclip- und Broker-Secrets gehören ausschließlich in ignorierte Runtime-
  Dateien. Keine Schlüssel in Agent-Prompts, Aufgaben oder Git eintragen.

## Voraussetzungen

| Betriebsart | Voraussetzungen |
|---|---|
| ZHF lokal | Python 3.10+, Bash 3.2+ (oder Fish-Launcher), Zugriff auf PyPI für Dependencies |
| Paperclip in Docker | Docker Engine + Docker Compose; Node auf dem Host ist nicht erforderlich |
| Paperclip nativ | Node.js ≥24.11.0 und npm/npx; Paperclip-CLI installiert oder via `npx` verfügbar |
| OpenCode Zen | OpenCode CLI und Netzwerkzugriff zur Katalogprüfung; fehlende Modelle sind optional, wenn ein lokaler LLM-Fallback aus der Agent-Laufzeit erreichbar ist |

## 1. Setup starten

Im Checkout-Root:

```bash
./setup-script.sh
```

Aus Fish:

```fish
fish setup-script.fish
```

Alternativ funktionieren `bash setup-script.sh` und der alte Aufruf
`bash scripts/setup.sh`. Das Skript:

1. prüft Python, Node, Docker/Compose, Paperclip-API/Container, OpenCode-Konfig
   und Modellkatalog;
2. legt `.venv` an, installiert `requirements.txt` und erstellt `.env` nur, wenn
   sie fehlt (vorhandene Werte bleiben unverändert);
3. schreibt `data/setup_report.json` ohne Secrets;
4. zeigt bei erkannter Docker-Installation ein interaktives Menü.

Nützliche Optionen:

```bash
./setup-script.sh --check-only
./setup-script.sh --mode docker
./setup-script.sh --mode native
./setup-script.sh --mode existing --api-url http://127.0.0.1:3100
./setup-script.sh --skip-pipeline
```

Ohne TTY startet `--mode auto` keine neue Paperclip-Instanz. Für Automatisierung
muss der Modus explizit gewählt werden.

## 2. Docker-Deployment (empfohlen)

```bash
./setup-script.sh --mode docker
```

Das Setup baut `Dockerfile.paperclip` auf dem offiziellen Paperclip-Produktions-
Image auf und installiert darin die ZHF-Python-Dependencies. Compose:

- bindet den Webserver standardmäßig nur an `127.0.0.1:3100`;
- nutzt Paperclips authentifizierten privaten Modus;
- erzwingt `ENVIRONMENT=paper`, `DRY_RUN=true`, Alpaca-Paper und BingX/Bitunix-Testnet unabhängig von ambienten Shell-Variablen;
- persistiert Paperclip-Daten unter `data/docker-paperclip/`;
- mappt den Container-Benutzer auf die Host-UID/GID, damit bind-gemountete Runtime-Daten beschreibbar bleiben;
- mountet den Checkout unter `/workspace/zhf`, damit `process`-Agenten dieselben
  Skripte, Prompts und Daten nutzen;
- erzeugt zufällige Secrets in `data/paperclip-compose.env` (Dateirechte 600).

Der Paperclip-Server erhält die vollständige Host-`.env` nicht als
Prozessumgebung. Der Checkout-Mount unter `/workspace/zhf` enthält die Datei
jedoch, damit manuell gestartete Python-Worker ihre ZHF-Konfiguration laden
können. Deshalb ist diese Docker-Installation eine **vertrauenswürdige
Einzelbetreiber-Umgebung**: Ein manuell gestarteter Agent kann auf dortige
Broker-Zugangsdaten zugreifen. Lade keine untrusted Mitglieder ein, halte den
Loopback-Bind und lasse Heartbeats deaktiviert, bis die Agenten-/Secret-Grenzen
bewusst geprüft wurden.

Der Compose-Build verwendet standardmäßig
`ghcr.io/paperclipai/paperclip:latest`. Für streng reproduzierbare oder
produktionskritische Installationen den Image-Tag oder Digest in
`data/paperclip-compose.env` pinnen, z. B. `PAPERCLIP_IMAGE=...:<version>`.

### Erstes Benutzerkonto und CLI-Kopplung

Nach dem ersten Start muss ein Mensch die Paperclip-Ersteinrichtung abschließen:

1. `http://localhost:3100` im Browser öffnen, Benutzer erstellen und die
   Board-Eigentümerschaft beanspruchen.
2. CLI-Gerät einmal koppeln (der Login-Link ist ein Geheimnis):

   ```bash
   docker exec -it --user node zhf-paperclip node /app/cli/dist/index.js auth login \
     --api-base http://127.0.0.1:3100
   ```

3. Erst danach die ZHF-Objekte provisionieren (siehe Schritt 4).

Das Setup druckt den Befehl auch im interaktiven Ablauf aus. Die erste menschliche
Identität kann nicht sicher im Shell-Skript angelegt werden; Paperclip verlangt
hierfür den interaktiven Account-/Board-Claim-Flow.

## 3. Native Paperclip-Installation

Wähle im Menü „Paperclip nativ starten“ oder nutze:

```bash
./setup-script.sh --mode native
```

Das Skript führt `paperclipai onboard --yes --install-service` aus (oder die
entsprechende `npx paperclipai`-Variante). Eine vorhandene Paperclip-Konfiguration
wird wiederverwendet. Für ein einzelnes Benutzerkonto reicht der lokale Trusted-
Modus. Zusätzliche menschliche Mitglieder benötigen Paperclip im authentifizierten
Modus und eine angemeldete Board-CLI-Identität.

## 4. ZHF-Firma, Agenten, Skills und Tasks provisionieren

```bash
./setup-script.sh --mode docker --provision-paperclip
# für native/andere laufende Instanzen:
./setup-script.sh --mode existing --provision-paperclip
```

Oder direkt:

```bash
# lokal/native
python -m scripts.paperclip_provision --action provision

# Paperclip-CLI im Docker-Container verwenden
python -m scripts.paperclip_provision \
  --action provision \
  --docker-container zhf-paperclip \
  --runtime-root /workspace/zhf \
  --python-path /opt/zhf-venv/bin/python
```

Die Versionierte Blaupause liegt in `config/paperclip_agents.json`. Der
Provisioner erstellt bzw. validiert:

- die Firma `zhf-trading`;
- sieben Agents: CEO, Research, Backtest, CRO/Risk, Execution, Cost Optimizer
  und Watchdog;
- die Markdown-Skills aus `agents/**/skills/` und ihre Zuordnung zu CEO/Research;
- vier idempotente Einrichtungsaufgaben.

Wiederholte Aufrufe duplizieren die Records nicht. Bestehende Records werden
anhand Firmenname, Agentenname, Skill-Slug und Task-Titel wiederverwendet; das
Skript löscht oder überschreibt keine manuell angelegten Records. Jeder neue
Agent erhält `heartbeat.enabled=false`; Tasks bleiben unzugewiesen und im
`backlog`. Schedule-Angaben in der Blaupause sind Vorschläge, keine aktive
Automatisierung.

Bei einer neu angelegten Firma wird die Einstellung für direkte Agent-Erstellung
nur während der Erstprovisionierung freigeschaltet und nach dem Anlegen des Rosters
wieder auf Board-Freigabe zurückgesetzt. Eine bestehende Firmenrichtlinie wird
niemals automatisch gelockert; bei einer unvollständigen Firma mit verpflichtender
Hire-Freigabe nennt das Skript die fehlenden Agents und stoppt.

Nach erfolgreichem Lauf werden Agenten, Skills und Tasks erneut von Paperclip
abgefragt. Abweichende oder fehlende Ressourcen führen zu einem Fehler mit
Diagnose statt zu einer stillen Erfolgsmeldung.

## 5. Neue menschliche Benutzer einladen

Paperclip erstellt menschliche Benutzer über Registrierung und einen
Einladungs-/Join-Flow; das Skript erzeugt keine Passwörter und versendet keine
E-Mail.

```bash
./setup-script.sh --mode docker --invite-user --invite-role operator
```

Rollen: `viewer`, `operator`, `admin`, `owner`. Das Ergebnis ist ein
kurzlebiger Einmal-Link, der privat geteilt und vom Betreiber in Paperclip
freigegeben werden muss. Für eine weitere Einladung muss eine Board-Identität
angemeldet sein.

## 6. OpenCode-Modellprüfung und Fallbacks

Prüfen:

```bash
python -m scripts.common.models
opencode models opencode
```

Die Modellauflösung liegt in `scripts/common/models.py`. Pro Agent wird in einer
geordneten Liste zuerst die bevorzugte Modell-ID und dann ein verfügbarer Zen-
Fallback gewählt. Optional lassen sich die Präferenzen über `.env` setzen:

```dotenv
OPENCODE_MODEL_CEO=opencode/nemotron-3-ultra-free
OPENCODE_MODEL_RESEARCH=opencode/big-pickle
OPENCODE_MODEL_RISK=opencode/minimax-m2.5-free
OPENCODE_MODEL_BACKTEST=opencode/gpt-5-nano
OPENCODE_MODEL_COST=opencode/mimo-v2.5-flash-free
```

Ein explizites Override wird nur verwendet, wenn es im erreichbaren Modellkatalog
steht. Wenn der Katalog nicht erreichbar ist, bleibt die Präferenz „unverified“;
bei einem echten OpenCode-Aufruffehler versucht `scripts/common/llm.py` automatisch
einen geladenen lokalen Server auf `LOCAL_LLM_BASE_URL` oder den Standard-Ports
LM Studio `1234`, Ollama `11434`, llama.cpp `8080` und vLLM `8000` — sofern der
Endpunkt aus genau dieser Laufzeit erreichbar ist. Im Docker-Modus bedeutet
`127.0.0.1` den Paperclip-Container, nicht den Host; der Container-Katalogcheck
meldet daher einen Host-only-LLM-Endpunkt nicht als Docker-Fallback. Für eine
Weiterleitung muss ein vom Container erreichbarer Host-Gateway-Endpunkt bewusst
konfiguriert werden. Ohne erreichbares LLM bleiben regelbasierte
Research-/Backtest-Pfade und der deterministische Execution-Code maßgeblich.

Wenn ein erfolgreicher Katalogaufruf keine der konfigurierten Modell-IDs enthält,
stoppt die Paperclip-Provisionierung statt ein veraltetes Modell zu hinterlegen.
Setze dann ein aktuell verfügbares `OPENCODE_MODEL_*` Override oder prüfe
`opencode models opencode`.

Die verfügbaren OpenCode-Modelle ändern sich unabhängig von ZHF. Die IDs in
`.opencode/opencode.json` sind Konfiguration, keine Garantie für aktuelle
Provider-Verfügbarkeit.

## 7. Pipeline und spätere Aktivierung

Der automatische Setup-Test ist absichtlich ein isolierter synthetischer Lauf.
Er ist nicht mit einem Live- oder Paper-Broker-Lauf zu verwechseln.

```bash
. .venv/bin/activate
python -m scripts.monitoring.synth_test --keep
python -m scripts.monitoring.watchdog
```

Erst nach Prüfung von `data/setup_report.json`, `data/synth/`, Limits und
Paper-Keys können Betreiber Heartbeats und Paperclip-Routinen manuell aktivieren.
`DRY_RUN=true`, `ALPACA_PAPER=true` und die menschlichen Approval-Regeln bleiben
Standard. Live-Trading benötigt separate, bewusste Konfiguration und Freigabe.

## Troubleshooting

| Befund | Ursache | Nächster Schritt |
|---|---|---|
| Docker-Menü zeigt Compose nicht verfügbar | Docker-Daemon aus oder Compose fehlt | `docker info` und `docker compose version` prüfen |
| Paperclip `/api/health` antwortet, Provisionierung meldet 401/403 | Board-CLI nicht angemeldet/gekoppelt | Benutzer-/Board-Claim im Browser abschließen und `auth login` für die CLI ausführen |
| `model not found` | Zen-Katalog hat die bevorzugte Modell-ID geändert | `python -m scripts.common.models` ausführen; verfügbare Alternative oder `OPENCODE_MODEL_*` setzen |
| OpenCode-Katalog nicht erreichbar | Offline, CLI fehlt oder Providerproblem | `data/setup_report.json` prüfen; lokalen `/v1/models`-Fallback starten oder Katalog später erneut testen |
| Paperclip-Agent kann Python nicht starten | Pfad/Runtime weicht von Setup ab | Agent-Konfiguration prüfen; Docker-Pfade sind `/workspace/zhf` und `/opt/zhf-venv/bin/python` |
| `paperclipai onboard` meldet alte Node-Version | Node zu alt für aktuelle CLI | Node.js ≥24.11.0 installieren oder Docker-Modus nutzen |

Paperclip- und Laufzeit-Logs sollten vor dem Teilen auf Tokens, Einladungslinks
und Broker-Informationen geprüft werden.
