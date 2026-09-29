#!/usr/bin/env bash
# ZHF bootstrap for Bash. From Fish, run `fish setup-script.fish` or
# `bash setup-script.sh`; do not source this script into an interactive shell.
set -Ee -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR" && pwd)"
cd "$REPO_ROOT"

VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$REPO_ROOT/scripts/common/version.py" | head -n 1)"
if [ -z "$VERSION" ]; then
  printf 'ERROR: could not read project version from scripts/common/version.py\n' >&2
  exit 1
fi
export ZHF_VERSION="$VERSION"
MODE="auto"
CHECK_ONLY=false
SKIP_DEPS=false
SKIP_PIPELINE=false
PROVISION_PAPERCLIP=false
INVITE_USER=false
INVITE_ROLE="operator"
INTERACTIVE=true
PAPERCLIP_URL="${PAPERCLIP_API_URL:-http://127.0.0.1:3100}"
CONTAINER_OVERRIDE=false
if [ -n "${PAPERCLIP_CONTAINER+x}" ] || [ -n "${PAPERCLIP_CONTAINER_NAME+x}" ]; then
  CONTAINER_OVERRIDE=true
fi
PAPERCLIP_CONTAINER="${PAPERCLIP_CONTAINER:-${PAPERCLIP_CONTAINER_NAME:-zhf-paperclip}}"
DOCKER_ENV_FILE="$REPO_ROOT/data/paperclip-compose.env"
MODEL_CATALOG_FILE="$REPO_ROOT/data/setup-opencode-models.txt"
MODEL_REPORT_FILE="$REPO_ROOT/data/setup_models.json"
SETUP_REPORT_FILE="$REPO_ROOT/data/setup_report.json"
PYTHON_BIN=""
PYTHON_VERSION="not-found"
DEPS_STATUS="not-checked"
NODE_VERSION="not-found"
NODE_OK=false
DOCKER_CLIENT=false
DOCKER_DAEMON=false
COMPOSE_KIND="none"
DOCKER_COMPOSE_UP=false
PAPERCLIP_PROVISIONED=false
PAPERCLIP_INVITE_CREATED=false
PAPERCLIP_HEALTHY=false
PAPERCLIP_KIND="not-selected"
PIPELINE_STATUS="skipped"
OPENCODE_VERSION="not-found"
MODEL_STATUS="unverified"
LOCAL_LLM_STATUS="unavailable"
MODEL_SUMMARY="{}"
ISSUES=()

if [ ! -t 0 ] || [ ! -t 1 ]; then
  INTERACTIVE=false
fi

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ -n "${TERM:-}" ] && [ "${TERM:-}" != "dumb" ]; then
  BOLD="$(tput bold 2>/dev/null || true)"
  GREEN="$(tput setaf 2 2>/dev/null || true)"
  YELLOW="$(tput setaf 3 2>/dev/null || true)"
  RED="$(tput setaf 1 2>/dev/null || true)"
  DIM="$(tput dim 2>/dev/null || true)"
  RESET="$(tput sgr0 2>/dev/null || true)"
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi

say() { printf '%b\n' "$*"; }
header() { say "\n${BOLD}== $* ==${RESET}"; }
ok() { say " ${GREEN}✔${RESET} $*"; }
warn() { say " ${YELLOW}⚠${RESET} $*"; }
err() { say " ${RED}✖${RESET} $*"; }
info() { say " ${DIM}·${RESET} $*"; }
issue() { ISSUES+=("$1"); warn "$1"; }
command_exists() { command -v "$1" >/dev/null 2>&1; }

on_error() {
  local status="$1"
  err "Unerwarteter Fehler (Exit-Code ${status}, Zeile ${BASH_LINENO[0]:-?})."
  err "Bitte prüfe die letzte Ausgabe; Zugangsdaten werden nicht in den Report geschrieben."
  exit "$status"
}
trap 'on_error $?' ERR

usage() {
  cat <<'USAGE'
ZHF Setup – idempotentes Bootstrap-Skript

Aufruf:
  ./setup-script.sh [Optionen]
  bash setup-script.sh [Optionen]
  fish setup-script.fish [Optionen]

Optionen:
  --mode auto|docker|native|existing|none
                         Paperclip-Ausführung wählen (Default: auto)
  --provision-paperclip  ZHF-Firma, Agenten, Skills und Setup-Tasks anlegen
  --invite-user          Paperclip-Einladung für einen neuen Menschen erzeugen
  --invite-role ROLE     viewer|operator|admin|owner (Default: operator)
  --api-url URL          Paperclip-API-Basis-URL (Default: http://127.0.0.1:3100)
  --container NAME       Docker-Containername für Paperclip (Default: zhf-paperclip)
  --skip-deps            Python-Pakete nicht installieren
  --skip-pipeline        Isolierten synthetischen Ende-zu-Ende-Test überspringen
  --check-only           Nur Voraussetzungen prüfen und Report schreiben
  --non-interactive      Keine Menüs/Bestätigungsfragen anzeigen
  -h, --help             Hilfe anzeigen

Ohne --mode werden in einem interaktiven Terminal verfügbare Paperclip-Wege
angeboten. Der automatisch gestartete Pipeline-Lauf ist immer synthetisch,
DRY_RUN und isoliert; echte Broker-Orders werden dadurch nicht ausgeführt.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode)
      [ "$#" -ge 2 ] || { err "--mode benötigt einen Wert"; exit 2; }
      MODE="$2"; shift 2 ;;
    --mode=*) MODE="${1#*=}"; shift ;;
    --provision-paperclip) PROVISION_PAPERCLIP=true; shift ;;
    --invite-user) INVITE_USER=true; shift ;;
    --invite-role)
      [ "$#" -ge 2 ] || { err "--invite-role benötigt einen Wert"; exit 2; }
      INVITE_ROLE="$2"; shift 2 ;;
    --invite-role=*) INVITE_ROLE="${1#*=}"; shift ;;
    --api-url)
      [ "$#" -ge 2 ] || { err "--api-url benötigt eine URL"; exit 2; }
      PAPERCLIP_URL="$2"; shift 2 ;;
    --api-url=*) PAPERCLIP_URL="${1#*=}"; shift ;;
    --container)
      [ "$#" -ge 2 ] || { err "--container benötigt einen Namen"; exit 2; }
      PAPERCLIP_CONTAINER="$2"; CONTAINER_OVERRIDE=true; shift 2 ;;
    --container=*) PAPERCLIP_CONTAINER="${1#*=}"; CONTAINER_OVERRIDE=true; shift ;;
    --skip-deps) SKIP_DEPS=true; shift ;;
    --skip-pipeline) SKIP_PIPELINE=true; shift ;;
    --check-only) CHECK_ONLY=true; shift ;;
    --non-interactive) INTERACTIVE=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unbekannte Option: $1"; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in
  auto|docker|native|existing|none) ;;
  *) err "Ungültiger Modus '$MODE' (auto|docker|native|existing|none)"; exit 2 ;;
esac
case "$INVITE_ROLE" in
  viewer|operator|admin|owner) ;;
  *) err "Ungültige Einladungsrolle '$INVITE_ROLE' (viewer|operator|admin|owner)"; exit 2 ;;
esac
PAPERCLIP_URL="${PAPERCLIP_URL%/}"

python_version_ok() {
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}
node_version_ok() {
  node -e 'const p=process.versions.node.split(".").map(Number); process.exit(p[0] > 24 || (p[0] === 24 && p[1] >= 11) ? 0 : 1)' >/dev/null 2>&1
}

port_open() {
  local port="$1"
  python3 - "$port" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(1.0)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

http_healthy() {
  local url="$1"
  python3 - "$url" <<'PY' >/dev/null 2>&1
import sys
from urllib.request import urlopen
try:
    with urlopen(sys.argv[1].rstrip("/") + "/api/health", timeout=2.0) as response:
        sys.exit(0 if 200 <= response.status < 300 else 1)
except Exception:
    sys.exit(1)
PY
}

open_browser() {
  local url="$1"
  if command_exists xdg-open; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif command_exists open; then
    open "$url" >/dev/null 2>&1 || true
  else
    info "Kein Browser-Launcher gefunden; öffne manuell: $url"
  fi
}

compose() {
  # The generated, mode-0600 file is the source of truth for deployment
  # parameters and secrets. Prevent ambient shell variables from overriding it.
  if [ "$COMPOSE_KIND" = "plugin" ]; then
    env -u PAPERCLIP_CONTAINER_NAME -u PAPERCLIP_PORT -u PAPERCLIP_IMAGE \
      -u PAPERCLIP_DATA_DIR -u PAPERCLIP_PUBLIC_URL -u USER_UID -u USER_GID \
      -u BETTER_AUTH_SECRET -u PAPERCLIP_AGENT_JWT_SECRET \
      -u PAPERCLIP_TOOL_ACTION_SIGNING_SECRET docker compose "$@"
  elif [ "$COMPOSE_KIND" = "standalone" ]; then
    env -u PAPERCLIP_CONTAINER_NAME -u PAPERCLIP_PORT -u PAPERCLIP_IMAGE \
      -u PAPERCLIP_DATA_DIR -u PAPERCLIP_PUBLIC_URL -u USER_UID -u USER_GID \
      -u BETTER_AUTH_SECRET -u PAPERCLIP_AGENT_JWT_SECRET \
      -u PAPERCLIP_TOOL_ACTION_SIGNING_SECRET docker-compose "$@"
  else
    return 127
  fi
}

check_environment() {
  header "1. System und Python"
  if command_exists python3; then
    PYTHON_BIN="$(command -v python3)"
    PYTHON_VERSION="$(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)"
    if python_version_ok; then
      ok "Python ${PYTHON_VERSION} (${PYTHON_BIN})"
    else
      err "Python ${PYTHON_VERSION} ist zu alt; mindestens Python 3.10 ist erforderlich."
      issue "python-too-old"
    fi
  else
    err "python3 wurde nicht gefunden; ZHF benötigt Python 3.10 oder neuer."
    issue "python-missing"
  fi

  if command_exists uname; then info "System: $(uname -s) $(uname -m)"; fi
  if command_exists df; then
    local free_disk
    free_disk="$(df -h "$REPO_ROOT" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
    [ -n "$free_disk" ] && info "Freier Speicher im Checkout: $free_disk"
  fi

  header "2. Node.js und Paperclip-Voraussetzungen"
  if command_exists node; then
    NODE_VERSION="$(node --version 2>/dev/null | sed 's/^v//' || echo unknown)"
    if node_version_ok; then
      NODE_OK=true
      ok "Node.js ${NODE_VERSION} (Paperclip benötigt mindestens 24.11.0)"
    else
      warn "Node.js ${NODE_VERSION}: zu alt für die aktuelle Paperclip-CLI (benötigt ≥24.11.0). Docker-Modus bleibt verfügbar."
      issue "node-too-old"
    fi
  else
    warn "Node.js fehlt. Native Paperclip/OpenCode benötigen Node.js ≥24.11.0; Docker-Modus kann ohne Node auf dem Host laufen."
    issue "node-missing"
  fi
  if command_exists npm; then info "npm $(npm --version 2>/dev/null || echo unknown)"; fi
  if command_exists npx; then info "npx verfügbar"; fi

  header "3. Docker / Compose"
  if command_exists docker; then
    DOCKER_CLIENT=true
    ok "Docker-CLI gefunden: $(docker --version 2>/dev/null || echo docker)"
    if docker info >/dev/null 2>&1; then
      DOCKER_DAEMON=true
      ok "Docker-Daemon ist erreichbar"
    else
      warn "Docker ist installiert, aber der Daemon ist nicht erreichbar (Docker starten/Berechtigungen prüfen)."
      issue "docker-daemon-unavailable"
    fi
    if docker compose version >/dev/null 2>&1; then
      COMPOSE_KIND="plugin"
      ok "Docker Compose Plugin verfügbar"
    elif command_exists docker-compose && docker-compose version >/dev/null 2>&1; then
      COMPOSE_KIND="standalone"
      ok "docker-compose verfügbar"
    else
      warn "Docker Compose fehlt; integrierte Docker-Orchestrierung kann nicht gestartet werden."
      issue "docker-compose-missing"
    fi
  else
    info "Docker nicht installiert (optional)"
  fi

  header "4. OpenCode und Modellkonfiguration"
  if command_exists opencode; then
    OPENCODE_VERSION="$(opencode --version 2>&1 | head -n 1 | sed 's/^[^0-9]*//' || echo unknown)"
    ok "OpenCode CLI gefunden (Version ${OPENCODE_VERSION})"
  else
    warn "OpenCode CLI nicht im Host-PATH. Das lokale LLM bleibt als Fallback möglich; Docker-Paperclip bringt OpenCode im Container mit."
    issue "opencode-missing"
  fi
  if [ -f "$REPO_ROOT/.opencode/opencode.json" ]; then
    if python3 -m json.tool "$REPO_ROOT/.opencode/opencode.json" >/dev/null 2>&1; then
      ok "OpenCode-Konfiguration ist valides JSON (.opencode/opencode.json)"
    else
      err ".opencode/opencode.json ist ungültig; bitte JSON-Syntax korrigieren."
      issue "opencode-config-invalid"
    fi
  else
    err ".opencode/opencode.json fehlt."
    issue "opencode-config-missing"
  fi

  if command_exists python3; then
    MODEL_SUMMARY="$(python3 "$REPO_ROOT/scripts/common/models.py" --json 2>/dev/null || echo '{}')"
    MODEL_STATUS="$(printf '%s' "$MODEL_SUMMARY" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("catalog_status", "unverified"))' 2>/dev/null || echo unverified)"
    LOCAL_LLM_STATUS="$(printf '%s' "$MODEL_SUMMARY" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("local_fallback") or {}).get("status", "unavailable"))' 2>/dev/null || echo unavailable)"
    if [ "$MODEL_STATUS" = "available" ]; then
      ok "OpenCode Zen-Modellkatalog erreichbar; bevorzugte Modelle/Fallbacks werden geprüft"
      printf '%s\n' "$MODEL_SUMMARY" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print("   - {}: {} ({})".format(k, (v.get("model") or "nicht verfügbar"), v.get("reason"))) for k,v in d.get("agents",{}).items() if k != "default"]' 2>/dev/null || true
    elif [ "$MODEL_STATUS" = "empty" ]; then
      warn "OpenCode antwortet, aber kein konfiguriertes Zen-Modell ist verfügbar. Es wird auf vorhandene lokale LLMs zurückgefallen; installiere/konfiguriere ein Modell oder prüfe den Katalog."
      issue "opencode-models-unavailable"
    else
      warn "Zen-Modellkatalog konnte nicht online verifiziert werden. Das Laufzeitsystem wählt bevorzugte Modelle und fällt bei Modellfehlern auf LM Studio/Ollama zurück."
      issue "opencode-models-unverified"
    fi
    if [ "$LOCAL_LLM_STATUS" = "available" ]; then
      ok "Lokaler LLM-Fallback ist erreichbar (LM Studio/Ollama-kompatibel)"
    else
      info "Kein lokaler LLM-Fallback aktiv; bei OpenCode-Ausfall bleiben Regelpfade/Report-Fallbacks verfügbar."
    fi
  fi

  header "5. Paperclip-Installation und Laufzeit"
  PAPERCLIP_HEALTHY=false
  if http_healthy "$PAPERCLIP_URL"; then
    PAPERCLIP_HEALTHY=true
    PAPERCLIP_KIND="external-or-native"
    ok "Paperclip API antwortet unter $PAPERCLIP_URL"
  elif port_open "${PAPERCLIP_URL##*:}"; then
    warn "Port aus PAPERCLIP_API_URL ist offen, aber /api/health antwortet nicht. Prüfe URL/Server-Logs."
    issue "paperclip-health-failed"
  else
    info "Kein Paperclip-Server an $PAPERCLIP_URL aktiv"
  fi
  if command_exists paperclipai; then
    info "Native Paperclip-CLI installiert: $(command -v paperclipai)"
    if [ "$PAPERCLIP_HEALTHY" != true ]; then
      info "CLI ist installiert; es läuft derzeit kein gesunder Server an der API-URL."
    fi
  elif [ -f "$HOME/.paperclip/instances/default/config.json" ] || [ -f "$HOME/.local/share/paperclip/instances/default/config.json" ]; then
    info "Native Paperclip-Instanzkonfiguration gefunden (Server derzeit nicht erreichbar)."
  fi
  if [ "$DOCKER_CLIENT" = true ] && [ "$DOCKER_DAEMON" = true ]; then
    local containers port detected_container
    containers="$(docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null || true)"
    port="${PAPERCLIP_URL##*:}"
    detected_container="$(printf '%s\n' "$containers" | awk -F'|' -v p="$port" 'tolower($2) ~ /paperclip/ && $3 ~ (":" p "->3100/tcp") {print $1; exit}')"
    if [ -n "$detected_container" ]; then
      info "Paperclip-Docker-Container am API-Port $port erkannt: $detected_container"
      PAPERCLIP_CONTAINER="$detected_container"
      PAPERCLIP_KIND="docker"
    fi
  fi

  header "6. Broker- und Laufzeit-Konfiguration"
  if [ -f "$REPO_ROOT/.env" ]; then
    ok ".env ist vorhanden"
    if [ "$CHECK_ONLY" = true ]; then
      info ".env-Berechtigungen werden im Check-only-Modus nicht verändert"
    elif chmod 600 "$REPO_ROOT/.env" 2>/dev/null; then
      info ".env-Dateirechte auf 600 gesetzt (API-Schlüssel geschützt)"
    else
      warn "Konnte .env-Dateirechte nicht auf 600 setzen; prüfe Dateisystemrechte."
      issue "env-permissions"
    fi
  else
    info ".env fehlt; sie wird bei der Einrichtung nicht-interaktiv aus .env.example angelegt."
  fi
}

prepare_python() {
  if [ "$CHECK_ONLY" = true ]; then return 0; fi
  if ! command_exists python3 || ! python_version_ok; then
    err "Ohne Python 3.10+ kann die ZHF-Umgebung nicht vorbereitet werden."
    return 1
  fi

  header "7. Python-Umgebung"
  if [ ! -d "$REPO_ROOT/.venv" ]; then
    info "Erstelle .venv …"
    if ! python3 -m venv "$REPO_ROOT/.venv"; then
      err "Erstellung von .venv fehlgeschlagen. Auf Debian/Ubuntu ggf. python3-venv installieren."
      issue "venv-create-failed"
      return 1
    fi
  else
    ok ".venv existiert bereits"
  fi
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  if [ ! -x "$PYTHON_BIN" ]; then
    err "Virtuelle Umgebung ist unvollständig: $PYTHON_BIN fehlt. .venv sichern/löschen und Setup erneut ausführen."
    issue "venv-python-missing"
    return 1
  fi
  ok "Virtuelle Umgebung bereit: $($PYTHON_BIN --version 2>&1)"

  if [ ! -f "$REPO_ROOT/.env" ]; then
    if [ ! -f "$REPO_ROOT/.env.example" ]; then
      err ".env.example fehlt; Konfiguration kann nicht angelegt werden."
      issue "env-template-missing"
      return 1
    fi
    umask 077
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    chmod 600 "$REPO_ROOT/.env" 2>/dev/null || true
    ok ".env aus .env.example erstellt; Broker-Schlüssel bleiben Platzhalter, bis du sie setzt"
  fi

  if [ "$SKIP_DEPS" = true ]; then
    DEPS_STATUS="skipped"
    warn "Python-Abhängigkeiten werden auf Wunsch nicht installiert (--skip-deps)."
  else
    header "8. Python-Abhängigkeiten"
    if "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input -r "$REPO_ROOT/requirements.txt"; then
      DEPS_STATUS="installed"
      ok "requirements.txt installiert"
    else
      err "Installation der Python-Abhängigkeiten fehlgeschlagen. Netzwerk/PyPI und pip-Ausgabe prüfen."
      issue "python-dependencies-failed"
      return 1
    fi
  fi

  if "$PYTHON_BIN" -c 'import pandas, numpy, requests, dotenv' >/dev/null 2>&1; then
    ok "Kernabhängigkeiten (pandas, numpy, requests, python-dotenv) importierbar"
  else
    err "Kernabhängigkeit fehlt oder ist defekt; requirements.txt Installation prüfen."
    issue "python-import-validation-failed"
    return 1
  fi

  if "$PYTHON_BIN" -m scripts.sync_env >/dev/null; then
    ok ".env mit .env.example abgeglichen (bestehende Werte wurden nicht überschrieben)"
  else
    warn "Konnte .env nicht mit .env.example abgleichen."
    issue "env-sync-failed"
  fi
  chmod 600 "$REPO_ROOT/.env" 2>/dev/null || true
}

ensure_docker_env() {
  if ! [[ "$PAPERCLIP_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    err "Paperclip-Containername enthält ungültige Zeichen: $PAPERCLIP_CONTAINER"
    return 1
  fi
  mkdir -p "$REPO_ROOT/data/docker-paperclip"
  if [ ! -f "$DOCKER_ENV_FILE" ]; then
    umask 077
    PAPERCLIP_CONTAINER_NAME="$PAPERCLIP_CONTAINER" python3 - "$DOCKER_ENV_FILE" <<'PY'
import os, secrets, sys
from pathlib import Path
path = Path(sys.argv[1])
values = {
    "USER_UID": os.environ.get("USER_UID") or str(os.getuid()),
    "USER_GID": os.environ.get("USER_GID") or str(os.getgid()),
    "PAPERCLIP_PORT": "3100",
    "PAPERCLIP_IMAGE": "ghcr.io/paperclipai/paperclip:latest",
    "PAPERCLIP_CONTAINER_NAME": os.environ.get("PAPERCLIP_CONTAINER_NAME") or "zhf-paperclip",
    "PAPERCLIP_DATA_DIR": "./data/docker-paperclip",
    "PAPERCLIP_PUBLIC_URL": "http://localhost:3100",
    "BETTER_AUTH_SECRET": secrets.token_hex(32),
    "PAPERCLIP_AGENT_JWT_SECRET": secrets.token_hex(32),
    "PAPERCLIP_TOOL_ACTION_SIGNING_SECRET": secrets.token_hex(32),
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("# Generated by setup-script.sh; do not commit or share.\n" + "".join(f"{k}={v}\n" for k,v in values.items()), encoding="utf-8")
path.chmod(0o600)
PY
    ok "Sichere Paperclip-Deployment-Secrets in data/paperclip-compose.env erzeugt (nicht im Git)"
  else
    chmod 600 "$DOCKER_ENV_FILE" 2>/dev/null || true
    # Ensure new secret variables are present without replacing existing values.
    python3 - "$DOCKER_ENV_FILE" "$PAPERCLIP_CONTAINER" "$CONTAINER_OVERRIDE" <<'PY'
import os, re, secrets, sys
from pathlib import Path
path = Path(sys.argv[1])
container_name = sys.argv[2]
container_override = sys.argv[3] == "true"
text = path.read_text(encoding="utf-8")
keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")}
if container_override:
    lines = []
    replaced = False
    for line in text.splitlines():
        if line.startswith("PAPERCLIP_CONTAINER_NAME="):
            if not replaced:
                lines.append(f"PAPERCLIP_CONTAINER_NAME={container_name}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"PAPERCLIP_CONTAINER_NAME={container_name}")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    keys.add("PAPERCLIP_CONTAINER_NAME")
missing = []
for key, default in (("USER_UID", os.environ.get("USER_UID") or str(os.getuid())),
                     ("USER_GID", os.environ.get("USER_GID") or str(os.getgid()))):
    if key not in keys:
        missing.append(f"{key}={default}")
for key in ("BETTER_AUTH_SECRET", "PAPERCLIP_AGENT_JWT_SECRET", "PAPERCLIP_TOOL_ACTION_SIGNING_SECRET"):
    if key not in keys:
        missing.append(f"{key}={secrets.token_hex(32)}")
if "PAPERCLIP_PORT" not in keys:
    missing.append("PAPERCLIP_PORT=3100")
if "PAPERCLIP_IMAGE" not in keys:
    missing.append("PAPERCLIP_IMAGE=ghcr.io/paperclipai/paperclip:latest")
if "PAPERCLIP_CONTAINER_NAME" not in keys:
    missing.append(f"PAPERCLIP_CONTAINER_NAME={container_name}")
if "PAPERCLIP_DATA_DIR" not in keys:
    missing.append("PAPERCLIP_DATA_DIR=./data/docker-paperclip")
if "PAPERCLIP_PUBLIC_URL" not in keys:
    missing.append("PAPERCLIP_PUBLIC_URL=http://localhost:3100")
if missing:
    with path.open("a", encoding="utf-8") as out:
        out.write("\n" + "\n".join(missing) + "\n")
text = path.read_text(encoding="utf-8")
port = next((line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("PAPERCLIP_PORT=")), "3100")
text = re.sub(r"(?m)^PAPERCLIP_PUBLIC_URL=http://localhost:3100$", f"PAPERCLIP_PUBLIC_URL=http://localhost:{port}", text)
path.write_text(text, encoding="utf-8")
path.chmod(0o600)
PY
    ok "Vorhandene Docker-Secrets beibehalten; fehlende Einstellungen idempotent ergänzt"
  fi

  local port configured_container
  configured_container="$(awk -F= '$1 == "PAPERCLIP_CONTAINER_NAME" {print $2; exit}' "$DOCKER_ENV_FILE" | tr -d '[:space:]' || true)"
  if [ -n "$configured_container" ]; then PAPERCLIP_CONTAINER="$configured_container"; fi
  if ! [[ "$PAPERCLIP_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    err "PAPERCLIP_CONTAINER_NAME in data/paperclip-compose.env enthält ungültige Zeichen."
    return 1
  fi
  port="$(awk -F= '$1 == "PAPERCLIP_PORT" {print $2; exit}' "$DOCKER_ENV_FILE" | tr -d '[:space:]' || true)"
  if ! printf '%s' "$port" | grep -Eq '^[0-9]{1,5}$' || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    err "PAPERCLIP_PORT in data/paperclip-compose.env muss zwischen 1 und 65535 liegen."
    return 1
  fi
  PAPERCLIP_URL="http://127.0.0.1:${port}"
  return 0
}

refresh_models_from_docker() {
  [ "$PAPERCLIP_KIND" = "docker" ] || return 0
  header "OpenCode-Modelle im Paperclip-Container"
  if docker exec --user node --workdir /workspace/zhf "$PAPERCLIP_CONTAINER" opencode models opencode > "$MODEL_CATALOG_FILE" 2>&1; then
    export ZHF_MODEL_CATALOG_FILE="$MODEL_CATALOG_FILE"
    MODEL_SUMMARY="$("$PYTHON_BIN" "$REPO_ROOT/scripts/common/models.py" --refresh --json --skip-local-probe 2>/dev/null || echo '{}')"
    printf '%s\n' "$MODEL_SUMMARY" > "$MODEL_REPORT_FILE"
    MODEL_STATUS="$(printf '%s' "$MODEL_SUMMARY" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("catalog_status", "unverified"))' 2>/dev/null || echo unverified)"
    LOCAL_LLM_STATUS="$(printf '%s' "$MODEL_SUMMARY" | "$PYTHON_BIN" -c 'import json,sys; print((json.load(sys.stdin).get("local_fallback") or {}).get("status", "unavailable"))' 2>/dev/null || echo unavailable)"
    if [ "$MODEL_STATUS" = "available" ]; then
      ok "Modellkatalog im Docker-Container geprüft; die Agents erhalten verfügbare Modelle bzw. Fallbacks."
      printf '%s\n' "$MODEL_SUMMARY" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); [print("   - {}: {} ({})".format(k, (v.get("model") or "nicht verfügbar"), v.get("reason"))) for k,v in d.get("agents",{}).items() if k != "default"]' 2>/dev/null || true
    elif [ "$MODEL_STATUS" = "empty" ]; then
      warn "Der Container-Katalog antwortet, enthält aber kein konfiguriertes Zen-Modell. Provisionierung stoppt statt ein veraltetes Modell einzutragen; setze ein verfügbares OPENCODE_MODEL_* Override."
      issue "container-models-empty"
    else
      warn "OpenCode-Modellkatalog im Container ist unverified; die Präferenz bleibt bestehen, aber kein Modell wird als verfügbar ausgegeben."
      issue "container-models-unverified"
    fi
  else
    warn "OpenCode-Modellkatalog im Container ist nicht erreichbar; die Modellpräferenz bleibt unverified. Host-lokale LLMs werden nicht automatisch in den Container weitergeleitet."
    issue "container-model-catalog-failed"
  fi
}

start_docker_paperclip() {
  if [ "$DOCKER_DAEMON" != true ] || [ "$COMPOSE_KIND" = "none" ]; then
    err "Docker-Modus benötigt einen erreichbaren Docker-Daemon und Docker Compose."
    issue "docker-requirements-not-met"
    return 1
  fi
  if [ ! -f "$REPO_ROOT/docker-compose.paperclip.yml" ] || [ ! -f "$REPO_ROOT/Dockerfile.paperclip" ]; then
    err "Docker-Artefakte fehlen (docker-compose.paperclip.yml / Dockerfile.paperclip)."
    issue "docker-config-missing"
    return 1
  fi
  ensure_docker_env || return 1
  header "Paperclip in Docker starten"
  info "Erzeuge/verifiziere ZHF-Runtime-Image. Broker-Secrets werden nur über die ignorierte .env-Datei eingelesen."
  if compose --env-file "$DOCKER_ENV_FILE" -f "$REPO_ROOT/docker-compose.paperclip.yml" up --detach --build; then
    PAPERCLIP_CONTAINER="$(awk -F= '$1 == "PAPERCLIP_CONTAINER_NAME" {print $2; exit}' "$DOCKER_ENV_FILE" | tr -d '[:space:]')"
    [ -n "$PAPERCLIP_CONTAINER" ] || PAPERCLIP_CONTAINER="zhf-paperclip"
    DOCKER_COMPOSE_UP=true
    PAPERCLIP_KIND="docker"
    ok "Docker-Compose hat den Paperclip-Dienst gestartet oder wiederverwendet"
  else
    err "Paperclip-Docker-Compose konnte nicht gestartet werden. Daten bleiben erhalten; Details mit docker compose logs prüfen."
    issue "paperclip-docker-start-failed"
    return 1
  fi

  local attempt
  for attempt in $(seq 1 90); do
    if http_healthy "$PAPERCLIP_URL"; then
      PAPERCLIP_HEALTHY=true
      ok "Paperclip API ist gesund: $PAPERCLIP_URL"
      refresh_models_from_docker
      return 0
    fi
    sleep 2
  done
  PAPERCLIP_HEALTHY=false
  err "Paperclip wurde gestartet, aber /api/health antwortet nach 180 Sekunden nicht. Logs prüfen: docker compose -f docker-compose.paperclip.yml logs --tail=100 paperclip"
  issue "paperclip-docker-health-timeout"
  return 1
}

start_native_paperclip() {
  if [ "$NODE_OK" != true ] || ! command_exists npx; then
    err "Native Paperclip-Installation erfordert Node.js ≥24.11.0 und npx. Nutze Docker oder installiere eine passende Node-Version."
    issue "native-paperclip-prerequisites-missing"
    return 1
  fi
  header "Native Paperclip starten"
  if command_exists paperclipai; then
    if paperclipai onboard --yes --install-service; then
      ok "Paperclip-Onboarding/Service abgeschlossen oder vorhandene Konfiguration wiederverwendet"
    else
      err "paperclipai onboard schlug fehl. Prüfe Paperclip-Dokumentation und System-Service-Rechte."
      issue "native-paperclip-onboard-failed"
      return 1
    fi
  else
    info "Nutze die offizielle Paperclip-CLI via npm (Node ≥24.11.0)"
    if npx --yes --registry https://registry.npmjs.org paperclipai onboard --yes --install-service; then
      ok "Paperclip-Onboarding/Service abgeschlossen oder vorhandene Konfiguration wiederverwendet"
    else
      err "npx paperclipai onboard schlug fehl. Prüfe Netzwerk, Node-Version und Paperclip-Diagnose."
      issue "native-paperclip-onboard-failed"
      return 1
    fi
  fi
  PAPERCLIP_KIND="native"
  local attempt
  for attempt in $(seq 1 45); do
    if http_healthy "$PAPERCLIP_URL"; then
      PAPERCLIP_HEALTHY=true
      ok "Native Paperclip API ist gesund: $PAPERCLIP_URL"
      return 0
    fi
    sleep 2
  done
  PAPERCLIP_HEALTHY=false
  warn "Paperclip-Setup ist vorhanden, aber die API antwortet noch nicht. Prüfe `paperclipai service status` und `paperclipai service logs`."
  issue "native-paperclip-health-timeout"
  return 1
}

choose_mode() {
  if [ "$MODE" != "auto" ]; then
    return 0
  fi
  if [ "$INTERACTIVE" != true ]; then
    if [ "$PAPERCLIP_HEALTHY" = true ]; then
      MODE="existing"
    else
      MODE="none"
      info "Nicht-interaktiver Lauf: keine neue Paperclip-Instanz ohne explizites --mode docker|native gestartet."
    fi
    return 0
  fi

  if [ "$DOCKER_CLIENT" = true ]; then
    header "Paperclip-Betriebsmodus (interaktives Menü)"
    if [ "$PAPERCLIP_HEALTHY" = true ]; then
      say "  1) Laufende Paperclip-Instanz wiederverwenden ($PAPERCLIP_URL)"
    else
      say "  1) Laufende Paperclip-Instanz wiederverwenden (derzeit nicht erreichbar)"
    fi
    if [ "$DOCKER_DAEMON" = true ] && [ "$COMPOSE_KIND" != none ]; then
      say "  2) Paperclip mit Docker Compose starten (empfohlen, authentifiziert, persistent)"
    else
      say "  2) Docker Compose starten (nicht verfügbar: Docker-Daemon/Compose prüfen)"
    fi
    if [ "$NODE_OK" = true ]; then
      say "  3) Paperclip nativ starten (Node-Service)"
    else
      say "  3) Native Installation (nicht verfügbar: Node.js ≥24.11.0 nötig)"
    fi
    say "  4) Nur lokale ZHF-Umgebung einrichten"
    printf 'Auswahl [1-4] (Default 1 bei laufender Instanz, sonst 4): '
    local answer
    read -r answer || answer=""
    if [ -z "$answer" ]; then
      if [ "$PAPERCLIP_HEALTHY" = true ]; then answer="1"; else answer="4"; fi
    fi
    case "$answer" in
      1) MODE="existing" ;;
      2) MODE="docker" ;;
      3) MODE="native" ;;
      4) MODE="none" ;;
      *) warn "Ungültige Auswahl; fahre mit lokaler ZHF-Umgebung fort."; MODE="none" ;;
    esac
  else
    header "Paperclip-Betriebsmodus"
    say "  1) Paperclip nativ starten (Node.js ≥24.11.0)"
    say "  2) Nur lokale ZHF-Umgebung einrichten"
    printf 'Auswahl [1-2] (Default 2): '
    local answer
    read -r answer || answer=""
    case "$answer" in
      1) MODE="native" ;;
      *) MODE="none" ;;
    esac
  fi
}

provision_paperclip() {
  if [ "$PAPERCLIP_HEALTHY" != true ]; then
    warn "ZHF-Firma kann erst provisioniert werden, wenn Paperclip /api/health meldet."
    issue "paperclip-not-healthy-for-provisioning"
    return 1
  fi
  if [ "$MODE" = "docker" ] || [ "$PAPERCLIP_KIND" = "docker" ]; then
    if [ "$DOCKER_COMPOSE_UP" != true ] && ! docker exec "$PAPERCLIP_CONTAINER" sh -lc 'test -f /workspace/zhf/scripts/run_pipeline.py && test -x /opt/zhf-venv/bin/python' >/dev/null 2>&1; then
      err "Der bestehende Paperclip-Container hat weder den ZHF-Checkout unter /workspace/zhf noch /opt/zhf-venv. Nutze das ZHF-Docker-Compose oder mounte/installiere die Worker-Runtime."
      issue "existing-paperclip-container-missing-zhf-runtime"
      return 1
    fi
    refresh_models_from_docker || true
    if "$PYTHON_BIN" -m scripts.paperclip_provision \
      --action provision \
      --api-base "$PAPERCLIP_URL" \
      --docker-container "$PAPERCLIP_CONTAINER" \
      --runtime-root /workspace/zhf \
      --python-path /opt/zhf-venv/bin/python; then
      ok "ZHF-Firma, Agenten, Skills und Setup-Tasks erfolgreich validiert"
      PAPERCLIP_PROVISIONED=true
      return 0
    fi
  else
    if "$PYTHON_BIN" -m scripts.paperclip_provision \
      --action provision \
      --api-base "$PAPERCLIP_URL" \
      --runtime-root "$REPO_ROOT" \
      --python-path "$PYTHON_BIN"; then
      ok "ZHF-Firma, Agenten, Skills und Setup-Tasks erfolgreich validiert"
      PAPERCLIP_PROVISIONED=true
      return 0
    fi
  fi
  err "ZHF-Provisionierung ist fehlgeschlagen. Bei authentifiziertem Paperclip zuerst den ersten Benutzer/Board-Owner anlegen und die CLI koppeln."
  issue "paperclip-provision-failed"
  return 1
}

invite_user() {
  if [ "$PAPERCLIP_HEALTHY" != true ]; then
    err "Benutzer-Einladungen benötigen eine laufende Paperclip-Instanz."
    issue "paperclip-not-running-for-invite"
    return 1
  fi
  local -a command_args
  command_args=(--action invite --role "$INVITE_ROLE" --api-base "$PAPERCLIP_URL")
  if [ "$MODE" = "docker" ] || [ "$PAPERCLIP_KIND" = "docker" ]; then
    command_args+=(--docker-container "$PAPERCLIP_CONTAINER")
  fi
  if "$PYTHON_BIN" -m scripts.paperclip_provision "${command_args[@]}"; then
    ok "Einladung erstellt. Teile den Einladungslink ausschließlich privat."
    PAPERCLIP_INVITE_CREATED=true
    return 0
  fi
  err "Einladung fehlgeschlagen. Authentifizierte Paperclip-Instanzen benötigen eine angemeldete Board-Identität."
  issue "paperclip-invite-failed"
  return 1
}

paperclip_interactive_followup() {
  [ "$INTERACTIVE" = true ] || return 0
  [ "$PAPERCLIP_HEALTHY" = true ] || return 0
  if [ "$MODE" = "docker" ]; then
    header "Erstbenutzer in Paperclip"
    say "Diese Docker-Instanz verwendet Authentifizierung. Beim ersten Start:"
    say "  1. Im Browser den ersten Benutzer anlegen und Board-Eigentümerschaft beanspruchen."
    say "  2. Die CLI einmal koppeln (der Login-Link ist ein geheimer Einmal-Link):"
    say "     docker exec -it --user node $PAPERCLIP_CONTAINER node /app/cli/dist/index.js auth login --api-base $PAPERCLIP_URL"
    say "  3. Danach ZHF provisionieren oder eine Benutzer-Einladung erstellen."
    open_browser "http://localhost:${PAPERCLIP_URL##*:}"
    if ! ask_yes_no "Sind Board-Owner angelegt und Paperclip-CLI gekoppelt?" "no"; then
      info "Paperclip-Provisionierung übersprungen. Nach Benutzer/Board-Setup erneut mit --mode docker --provision-paperclip starten."
      return 0
    fi
  fi
  if [ "$PROVISION_PAPERCLIP" != true ] && [ "$INVITE_USER" != true ]; then
    if ask_yes_no "ZHF-Firma mit Agenten, Skills und Setup-Tasks jetzt idempotent anlegen" "yes"; then
      PROVISION_PAPERCLIP=true
    fi
  fi
  if [ "$PROVISION_PAPERCLIP" = true ] && [ "$PAPERCLIP_PROVISIONED" != true ]; then
    provision_paperclip || true
  fi
  if [ "$INVITE_USER" != true ] && [ "$PROVISION_PAPERCLIP" = true ]; then
    if ask_yes_no "Einladung für einen zusätzlichen Paperclip-Benutzer erstellen" "no"; then
      INVITE_USER=true
      printf 'Rolle [viewer/operator/admin/owner] (Default operator): '
      local role
      read -r role || role=""
      [ -n "$role" ] && INVITE_ROLE="$role"
    fi
  fi
  if [ "$INVITE_USER" = true ] && [ "$PAPERCLIP_INVITE_CREATED" != true ]; then invite_user || true; fi
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-no}"
  local answer suffix
  if [ "$default" = "yes" ]; then suffix="[Y/n]"; else suffix="[y/N]"; fi
  printf '%s %s: ' "$prompt" "$suffix"
  read -r answer || answer=""
  answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
  if [ -z "$answer" ]; then [ "$default" = "yes" ]; else [[ "$answer" =~ ^(y|yes|j|ja)$ ]]; fi
}

run_safe_pipeline() {
  if [ "$CHECK_ONLY" = true ] || [ "$SKIP_PIPELINE" = true ]; then
    PIPELINE_STATUS="skipped"
    info "Isolierter Synth-Pipeline-Test wurde übersprungen."
    return 0
  fi
  if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    PIPELINE_STATUS="skipped"
    issue "synthetic-pipeline-not-run-python-unavailable"
    return 1
  fi
  header "Sicherer automatischer Pipeline-Test"
  say "ZHF_SYNTH=1 · ZHF_SKIP_LLM=1 · DRY_RUN=true · State=data/synth"
  say "Es werden keine Broker-API-Aufrufe und keine echten Orders ausgeführt."
  if ZHF_SYNTH=1 ZHF_SKIP_LLM=1 DRY_RUN=true "$PYTHON_BIN" -m scripts.monitoring.synth_test --keep; then
    PIPELINE_STATUS="passed"
    ok "Isolierter synthetischer Pipeline-Lauf erfolgreich"
    return 0
  fi
  PIPELINE_STATUS="failed"
  err "Synthetischer Ende-zu-Ende-Test lieferte Fehler; Report/Logs in data/synth prüfen."
  issue "synthetic-pipeline-failed"
  return 1
}

write_report() {
  [ "$CHECK_ONLY" = true ] || mkdir -p "$REPO_ROOT/data"
  command_exists python3 || return 1
  SETUP_ISSUES_JSON="$(printf '%s\n' "${ISSUES[@]}" | python3 -c 'import json,sys; print(json.dumps([s.strip() for s in sys.stdin if s.strip()]))')"
  export ZHF_SETUP_VERSION="$VERSION"
  export ZHF_SETUP_REPO_ROOT="$REPO_ROOT"
  export ZHF_SETUP_PYTHON_VERSION="$PYTHON_VERSION"
  export ZHF_SETUP_DEPS_STATUS="$DEPS_STATUS"
  export ZHF_SETUP_PYTHON_PATH="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
  export ZHF_SETUP_NODE_VERSION="$NODE_VERSION"
  export ZHF_SETUP_NODE_OK="$NODE_OK"
  export ZHF_SETUP_DOCKER_CLIENT="$DOCKER_CLIENT"
  export ZHF_SETUP_DOCKER_DAEMON="$DOCKER_DAEMON"
  export ZHF_SETUP_COMPOSE_KIND="$COMPOSE_KIND"
  export ZHF_SETUP_OPENCODE_VERSION="$OPENCODE_VERSION"
  export ZHF_SETUP_MODEL_STATUS="$MODEL_STATUS"
  export ZHF_SETUP_LOCAL_LLM_STATUS="$LOCAL_LLM_STATUS"
  export ZHF_SETUP_MODEL_REPORT="$MODEL_REPORT_FILE"
  export ZHF_SETUP_MODEL_SUMMARY="$MODEL_SUMMARY"
  export ZHF_SETUP_PAPERCLIP_KIND="$PAPERCLIP_KIND"
  export ZHF_SETUP_PAPERCLIP_HEALTHY="$PAPERCLIP_HEALTHY"
  export ZHF_SETUP_PROVISIONED="$PAPERCLIP_PROVISIONED"
  export ZHF_SETUP_INVITE_CREATED="$PAPERCLIP_INVITE_CREATED"
  export ZHF_SETUP_PAPERCLIP_URL="$PAPERCLIP_URL"
  export ZHF_SETUP_PIPELINE_STATUS="$PIPELINE_STATUS"
  export ZHF_SETUP_ISSUES="$SETUP_ISSUES_JSON"
  export ZHF_SETUP_REPORT_FILE="$SETUP_REPORT_FILE"
  if python3 - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

def safe_url(raw):
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[invalid-url]"

model_report = {}
try:
    model_report = json.loads(os.environ.get("ZHF_SETUP_MODEL_SUMMARY", "{}"))
except json.JSONDecodeError:
    try:
        model_report = json.loads(Path(os.environ["ZHF_SETUP_MODEL_REPORT"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
try:
    issues = json.loads(os.environ.get("ZHF_SETUP_ISSUES", "[]"))
except json.JSONDecodeError:
    issues = []
report = {
    "version": os.environ["ZHF_SETUP_VERSION"],
    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "repo_root": os.environ["ZHF_SETUP_REPO_ROOT"],
    "python": {
        "version": os.environ["ZHF_SETUP_PYTHON_VERSION"],
        "interpreter": os.environ["ZHF_SETUP_PYTHON_PATH"],
        "dependencies": os.environ["ZHF_SETUP_DEPS_STATUS"],
        "venv_ready": (Path(os.environ["ZHF_SETUP_REPO_ROOT"]) / ".venv" / "bin" / "python").is_file(),
    },
    "node": {
        "version": os.environ["ZHF_SETUP_NODE_VERSION"],
        "meets_paperclip_minimum": os.environ["ZHF_SETUP_NODE_OK"] == "true",
    },
    "docker": {
        "client": os.environ["ZHF_SETUP_DOCKER_CLIENT"] == "true",
        "daemon": os.environ["ZHF_SETUP_DOCKER_DAEMON"] == "true",
        "compose": os.environ["ZHF_SETUP_COMPOSE_KIND"],
    },
    "opencode": {
        "version": os.environ["ZHF_SETUP_OPENCODE_VERSION"],
        "catalog_status": os.environ["ZHF_SETUP_MODEL_STATUS"],
        "local_fallback_status": os.environ["ZHF_SETUP_LOCAL_LLM_STATUS"],
        "catalog_source": model_report.get("catalog_source"),
        "available_models": model_report.get("available_models", []),
        "selected_models": model_report.get("selected_models", {}),
        "agents": model_report.get("agents", {}),
    },
    "paperclip": {
        "mode": os.environ["ZHF_SETUP_PAPERCLIP_KIND"],
        "healthy": os.environ["ZHF_SETUP_PAPERCLIP_HEALTHY"] == "true",
        "provisioned": os.environ["ZHF_SETUP_PROVISIONED"] == "true",
        "invite_created": os.environ["ZHF_SETUP_INVITE_CREATED"] == "true",
        "api_url": safe_url(os.environ["ZHF_SETUP_PAPERCLIP_URL"]),
    },
    "pipeline": {
        "status": os.environ["ZHF_SETUP_PIPELINE_STATUS"],
        "mode": "synthetic-isolated",
        "real_orders_sent": False,
    },
    "issues": issues,
}
path = Path(os.environ["ZHF_SETUP_REPORT_FILE"])
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(path)
PY
  then
    ok "Setup-Report geschrieben: data/setup_report.json"
  else
    err "Setup-Report konnte nicht geschrieben/validiert werden."
    issue "setup-report-write-failed"
    return 1
  fi
}

# -----------------------------------------------------------------------------
header "ZHF Setup v${VERSION}"
say "Repository: $REPO_ROOT"
check_environment

if [ "$CHECK_ONLY" = true ]; then
  # A read-only check does not install dependencies, write .env, or start services.
  write_report || true
  header "Prüfung abgeschlossen"
  if [ "${#ISSUES[@]}" -eq 0 ]; then
    ok "Keine Probleme erkannt."
    exit 0
  fi
  warn "${#ISSUES[@]} Hinweis(e): ${ISSUES[*]}"
  for item in "${ISSUES[@]}"; do
    case "$item" in
      python-missing|python-too-old|setup-report-write-failed|opencode-config-invalid|opencode-config-missing) exit 1 ;;
    esac
  done
  exit 0
fi

if ! prepare_python; then
  write_report || true
  exit 1
fi

# Use the venv for all following project operations. Query host OpenCode again
# after config/env setup so .env model overrides are reflected in the report.
if [ -n "$PYTHON_BIN" ]; then
  MODEL_SUMMARY="$("$PYTHON_BIN" "$REPO_ROOT/scripts/common/models.py" --refresh --json 2>/dev/null || echo '{}')"
  printf '%s\n' "$MODEL_SUMMARY" > "$MODEL_REPORT_FILE"
  MODEL_STATUS="$(printf '%s' "$MODEL_SUMMARY" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("catalog_status", "unverified"))' 2>/dev/null || echo unverified)"
  LOCAL_LLM_STATUS="$(printf '%s' "$MODEL_SUMMARY" | "$PYTHON_BIN" -c 'import json,sys; print((json.load(sys.stdin).get("local_fallback") or {}).get("status", "unavailable"))' 2>/dev/null || echo unavailable)"
fi

choose_mode
case "$MODE" in
  docker)
    if start_docker_paperclip; then PAPERCLIP_HEALTHY=true; fi ;;
  native)
    if start_native_paperclip; then PAPERCLIP_HEALTHY=true; fi ;;
  existing)
    if [ "$PAPERCLIP_HEALTHY" != true ]; then
      if http_healthy "$PAPERCLIP_URL"; then
        PAPERCLIP_HEALTHY=true
        PAPERCLIP_KIND="external-or-native"
      else
        err "Kein gesunder Paperclip-Server unter $PAPERCLIP_URL. Passe --api-url an oder starte Paperclip."
        issue "selected-paperclip-instance-unhealthy"
      fi
    fi ;;
  none) PAPERCLIP_KIND="not-started" ;;
esac

if [ "$INTERACTIVE" = true ] && [ "$PAPERCLIP_HEALTHY" = true ]; then
  # Authenticated Docker mode intentionally requires a human owner claim and
  # CLI pairing before control-plane writes can be made.
  paperclip_interactive_followup
fi

if [ "$PROVISION_PAPERCLIP" = true ] && [ "$PAPERCLIP_HEALTHY" = true ] && [ "$PAPERCLIP_PROVISIONED" != true ]; then
  provision_paperclip || true
fi
if [ "$INVITE_USER" = true ] && [ "$PAPERCLIP_HEALTHY" = true ] && [ "$PAPERCLIP_INVITE_CREATED" != true ]; then
  invite_user || true
fi

pipeline_rc=0
run_safe_pipeline || pipeline_rc=$?
write_report || true

header "Zusammenfassung"
say "Paperclip: $PAPERCLIP_KIND ($([ "$PAPERCLIP_HEALTHY" = true ] && echo gesund || echo nicht gestartet/gesundheit nicht bestätigt))"
say "OpenCode-Modellkatalog: $MODEL_STATUS | lokaler LLM-Fallback: $LOCAL_LLM_STATUS"
say "Synthetische Pipeline: $PIPELINE_STATUS"
if [ "${#ISSUES[@]}" -gt 0 ]; then
  warn "Hinweise: ${ISSUES[*]}"
fi
say ""
say "Nächste Schritte:"
say "  1. Broker-Schlüssel und Limits in .env prüfen; DRY_RUN=true beibehalten."
if [ "$PAPERCLIP_HEALTHY" = true ]; then
  say "  2. Paperclip öffnen: http://localhost:${PAPERCLIP_URL##*:}"
  if [ "$PAPERCLIP_KIND" = "docker" ]; then
    say "     Bei der Ersteinrichtung Benutzer/Board-Owner anlegen und CLI koppeln."
    say "  3. ZHF-Firma provisionieren: ./setup-script.sh --mode docker --provision-paperclip"
  elif [ "$PAPERCLIP_KIND" = "native" ]; then
    say "  3. ZHF-Firma provisionieren: ./setup-script.sh --mode existing --provision-paperclip"
  else
    say "  3. ZHF-Firma provisionieren: ./setup-script.sh --mode existing --api-url <URL> --provision-paperclip"
  fi
  say "  4. Daten/Report prüfen: data/setup_report.json und data/synth/."
  say "  5. Heartbeats erst nach manueller Paper-/Risk-Prüfung aktivieren. Live-Trading wird nie automatisch freigeschaltet."
else
  say "  2. Paperclip starten mit --mode docker (Docker/Compose) oder --mode native (Node.js ≥24.11.0)."
  say "  3. Daten/Report prüfen: data/setup_report.json und data/synth/."
  say "  4. Heartbeats erst nach manueller Paper-/Risk-Prüfung aktivieren. Live-Trading wird nie automatisch freigeschaltet."
fi

if [ "$pipeline_rc" -ne 0 ]; then exit "$pipeline_rc"; fi
if [ "${#ISSUES[@]}" -gt 0 ]; then
  # Optional integrations and an unverified remote catalog are warnings. Only
  # failures recorded above that invalidate core setup should fail the script.
  for item in "${ISSUES[@]}"; do
    case "$item" in
      python-*|venv-*|env-template-missing|setup-report-write-failed|opencode-config-invalid|opencode-config-missing|docker-requirements-not-met|docker-config-missing|paperclip-docker-start-failed|paperclip-docker-health-timeout|native-paperclip-prerequisites-missing|native-paperclip-onboard-failed|native-paperclip-health-timeout|selected-paperclip-instance-unhealthy|paperclip-not-healthy-for-provisioning|paperclip-not-running-for-invite|existing-paperclip-container-missing-zhf-runtime|paperclip-provision-failed|paperclip-invite-failed|synthetic-pipeline-failed)
        exit 1 ;;
    esac
  done
fi
exit 0
