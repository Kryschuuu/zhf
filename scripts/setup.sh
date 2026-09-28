#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# ZHF Trading System – Setup
#
# Was das Skript macht:
#   1. Erkennt bereits vorhandene Installationen von:
#        - Python (≥3.10) + venv
#        - Node.js (≥18, für Paperclip/OpenCode)
#        - OpenCode CLI (opencode-ai)
#        - Paperclip CLI / lokaler Paperclip-Server
#        - LM Studio (native / als Prozess / Desktop)
#        - Ollama (native / Docker)
#        - PostgreSQL (native / Docker, für Paperclip)
#        - Docker / Docker Compose / Podman
#      Vorhandene Installationen werden wiederverwendet, statt sie neu zu
#      installieren.
#   2. Legt die Python-virtuelle Umgebung in `.venv` an und installiert
#      die Abhängigkeiten aus `requirements.txt`.
#   3. Erzeugt die Daten-Verzeichnisstruktur.
#   4. Schreibt eine Konfigurations-Zusammenfassung nach `data/setup_report.json`.
#
# Verwendung:  bash scripts/setup.sh
# -----------------------------------------------------------------------------
set -euo pipefail

# Ins Repo-Root wechseln (egal wo das Skript aufgerufen wird)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Terminal-Farben (optional)
if [ -t 1 ]; then
  BOLD="$(tput bold)"; DIM="$(tput dim)"; GREEN="$(tput setaf 2)"; YELLOW="$(tput setaf 3)"; RED="$(tput setaf 1)"; RESET="$(tput sgr0)"
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

say()     { printf "%b\n" "$*"; }
ok()      { say " ${GREEN}✔${RESET} $*"; }
warn()    { say " ${YELLOW}⚠${RESET} $*"; }
err()     { say " ${RED}✖${RESET} $*"; }
info()    { say " ${DIM}·${RESET} $*"; }
header()  { say "\n${BOLD}== $* ==${RESET}"; }

declare -A FOUND=()
declare -a ISSUES=()

command_exists() { command -v "$1" >/dev/null 2>&1; }
port_open()     {
  # port_open <host> <port>  – Return 0 wenn TCP-Port offen ist
  if command_exists nc; then
    nc -z -w1 "$1" "$2" >/dev/null 2>&1
  elif command_exists bash; then
    timeout 1 bash -c "cat </dev/null >/dev/tcp/$1/$2" >/dev/null 2>&1
  else
    return 1
  fi
}
first_existing_path() {
  for p in "$@"; do [ -e "$p" ] && { echo "$p"; return 0; }; done
  return 1
}
systemd_unit_active() { command_exists systemctl && systemctl is-active --quiet "$1" 2>/dev/null; }
container_running() {
  local name="$1"
  if command_exists docker; then
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qi "^${name}$" && return 0
  fi
  if command_exists podman; then
    podman ps --format '{{.Names}}' 2>/dev/null | grep -qi "^${name}$" && return 0
  fi
  return 1
}

# -----------------------------------------------------------------------------
header "ZHF Trading System Setup v$(cat scripts/common/version.py 2>/dev/null | grep __version__ | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
say   "Arbeitsverzeichnis: $REPO_ROOT"

# -----------------------------------------------------------------------------
header "1. Betriebssystem"
if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "${PRETTY_NAME:-$NAME} ($(uname -m))"
else
  ok "$(uname -s) $(uname -r) ($(uname -m))"
fi

TOTAL_MEM_MB=$(free -m 2>/dev/null | awk '/^Mem:/ {print $2}' || echo "?")
FREE_DISK_GB=$(df -BG "$REPO_ROOT" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G' || echo "?")
info "RAM: ${TOTAL_MEM_MB} MB | Freier Speicher: ${FREE_DISK_GB} GB"
if [ "$TOTAL_MEM_MB" != "?" ] && [ "$TOTAL_MEM_MB" -lt 6000 ]; then
  warn "Weniger als 6 GB RAM – lokaler LLM-Betrieb wird eingeschränkt sein (OpenCode-Free-Tier wird empfohlen)"
fi

# -----------------------------------------------------------------------------
header "2. Python"
if ! command_exists python3; then
  err "python3 nicht gefunden – bitte zuerst installieren"
  ISSUES+=("python3-missing")
else
  PY_VERSION=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
  PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
  PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
  if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "Python ${PY_VERSION} ist zu alt (benötigt ≥3.10)"
    ISSUES+=("python-too-old")
  else
    ok "Python ${PY_VERSION} ($(which python3))"
  fi
fi

if [ ! -d "$REPO_ROOT/.venv" ]; then
  info "Erstelle .venv …"
  python3 -m venv .venv
else
  ok ".venv existiert bereits"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
ok "venv aktiv – $(python --version)"

# -----------------------------------------------------------------------------
header "3. Python-Abhängigkeiten"
pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# Prüfe ob alle requirements erfüllt sind
MISSING_DEPS=()
while IFS= read -r pkg; do
  pkg_name=$(echo "$pkg" | sed -E 's/[<>=!~].*//')
  if ! python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('${pkg_name//-/_}') else 1)" >/dev/null 2>&1; then
    # Mapping für Import-Namen, die vom Paketnamen abweichen:
    case "$pkg_name" in
      python-dotenv) python -c "import dotenv" 2>/dev/null && continue ;;
      alpaca-py)     python -c "import alpaca" 2>/dev/null && continue ;;
      ccxt)          python -c "import ccxt" 2>/dev/null && continue ;;
      pandas)        python -c "import pandas" 2>/dev/null && continue ;;
      numpy)         python -c "import numpy" 2>/dev/null && continue ;;
      ta)            python -c "import ta" 2>/dev/null && continue ;;
    esac
    MISSING_DEPS+=("$pkg")
  fi
done < requirements.txt

if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
  info "Installiere ${#MISSING_DEPS[@]} fehlende Pakete …"
  pip install --quiet -r requirements.txt && ok "Alle Pakete installiert"
else
  ok "Alle Python-Abhängigkeiten sind bereits installiert"
fi

# -----------------------------------------------------------------------------
header "4. Node.js (für OpenCode / Paperclip)"
if command_exists node; then
  NODE_VERSION=$(node --version | sed 's/^v//')
  NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
  ok "Node.js ${NODE_VERSION} ($(which node))"
  if [ "$NODE_MAJOR" -lt 18 ]; then
    warn "Node.js ${NODE_VERSION} ist zu alt – Paperclip benötigt ≥18 (empfohlen ≥22)"
    ISSUES+=("node-too-old")
  fi
else
  warn "Node.js nicht gefunden – OpenCode/Paperclip werden nicht funktionieren"
  info "Installationsempfehlung: nvm (https://github.com/nvm-sh/nvm) oder via Paketmanager"
  ISSUES+=("node-missing")
fi

if command_exists npm; then
  ok "npm $(npm --version)"
fi

# -----------------------------------------------------------------------------
header "5. OpenCode CLI (primärer LLM-Harness)"
FOUND_OPENCODE="no"
if command_exists opencode; then
  OC_VERSION=$(opencode --version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "?")
  ok "OpenCode ${OC_VERSION} ($(which opencode))"
  FOUND_OPENCODE="native"
else
  # Suche auch global via npm
  if [ -d /usr/local/lib/node_modules/opencode-ai ] || [ -d "${HOME}/.npm-global/lib/node_modules/opencode-ai" ]; then
    ok "OpenCode via npm gefunden, aber nicht im PATH – npm bin ins PATH aufnehmen"
    FOUND_OPENCODE="npm"
  else
    warn "OpenCode nicht installiert. Installation mit:"
    info "  npm install -g opencode-ai"
    info "  oder: curl -fsSL https://opencode.ai/install | bash"
    ISSUES+=("opencode-missing")
  fi
fi

if [ -f "$REPO_ROOT/.opencode/opencode.json" ]; then
  ok "OpenCode-Konfiguration vorhanden (.opencode/opencode.json)"
else
  warn ".opencode/opencode.json fehlt – Free-Modell-Konfiguration wird empfohlen"
fi

# -----------------------------------------------------------------------------
header "6. Paperclip (Agent-Orchestrierung)"
FOUND_PAPERCLIP="no"
if command_exists npx; then
  ok "npx verfügbar – Paperclip kann bei Bedarf gestartet werden (npx paperclipai onboard)"
  FOUND_PAPERCLIP="npx"
fi
if command_exists paperclip; then
  ok "Paperclip CLI installiert ($(which paperclip))"
  FOUND_PAPERCLIP="native"
fi

# Läuft bereits ein Paperclip-Server auf Port 3100?
if port_open 127.0.0.1 3100; then
  ok "Paperclip-Server läuft auf http://localhost:3100"
  FOUND_PAPERCLIP="running"
else
  info "Kein laufender Paperclip-Server auf Port 3100 – später mit 'npx paperclipai onboard' starten"
fi

# Suche bereits vorhandene Paperclip-Datenverzeichnisse
for p in ~/.paperclip ~/.local/share/paperclip ~/.config/paperclip; do
  if [ -d "$p" ]; then
    ok "Paperclip-Daten vorhanden in $p"
    FOUND[paperclip_data]="$p"
    break
  fi
done

# -----------------------------------------------------------------------------
header "7. Docker / Podman (für optionale Container-Dienste)"
if command_exists docker; then
  ok "docker $(docker --version 2>&1 | sed 's/Docker version //;s/,.*//')"
  FOUND[docker]="$(which docker)"
elif command_exists podman; then
  ok "podman $(podman --version | awk '{print $3}')"
  FOUND[docker]="$(which podman) (Podman-kompatibel)"
else
  info "Weder docker noch podman gefunden (optional, für Container-basierte Dienste)"
fi

# -----------------------------------------------------------------------------
header "8. PostgreSQL (für Paperclip)"
FOUND_PG="no"
if command_exists psql; then
  ok "psql Client $(psql --version | awk '{print $3}') ($(which psql))"
  FOUND_PG="psql"
fi
if command_exists postgres || command_exists pg_ctl || systemd_unit_active postgresql; then
  ok "PostgreSQL-Server native installiert"
  [ "$FOUND_PG" = "no" ] && FOUND_PG="native" || FOUND_PG="native+psql"
  if port_open 127.0.0.1 5432; then
    ok "PostgreSQL lauscht auf 127.0.0.1:5432"
  fi
fi
if container_running postgres || container_running paperclip-postgres || container_running pg; then
  ok "PostgreSQL läuft als Docker-Container"
  FOUND_PG="docker"
fi
if [ "$FOUND_PG" = "no" ]; then
  info "Kein PostgreSQL-Server gefunden – Paperclip nutzt bei Onboarding standardmässig eine eingebettete DB (kein separater Dienst nötig)"
fi

# -----------------------------------------------------------------------------
header "9. LM Studio (lokaler LLM-Server – Fallback)"
FOUND_LMSTUDIO="no"
# Native Installationen
for p in /usr/bin/LM\ Studio /opt/LM\ Studio /usr/local/bin/lms /usr/local/bin/lmstudio \
         /Applications/LM\ Studio.app ~/Applications/LM\ Studio.app ~/.local/share/lm-studio \
         ~/.lmstudio; do
  if [ -e "$p" ]; then
    ok "LM Studio gefunden: $p"
    FOUND_LMSTUDIO="native"; break
  fi
done
if command_exists lms; then
  ok "LM Studio CLI (lms) verfügbar"
  FOUND_LMSTUDIO="lms"
fi
if port_open 127.0.0.1 1234; then
  ok "LM Studio API-Server lauscht auf Port 1234"
  FOUND_LMSTUDIO="running"
fi
if container_running lmstudio || container_running lm-studio; then
  ok "LM Studio läuft als Docker-Container"
  FOUND_LMSTUDIO="docker"
fi
if [ "$FOUND_LMSTUDIO" = "no" ]; then
  info "LM Studio nicht gefunden – ist für den Primärbetrieb nicht erforderlich (OpenCode-Free-Tier). Nur als Offline-Fallback sinnvoll."
fi

# -----------------------------------------------------------------------------
header "10. Ollama (alternative lokale LLM-Laufzeit)"
FOUND_OLLAMA="no"
if command_exists ollama; then
  OLLAMA_VER=$(ollama --version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "?")
  ok "Ollama ${OLLAMA_VER} ($(which ollama))"
  FOUND_OLLAMA="native"
  if systemd_unit_active ollama; then
    ok "Ollama-Systemdienst läuft"
  fi
fi
if port_open 127.0.0.1 11434; then
  ok "Ollama API lauscht auf Port 11434"
  FOUND_OLLAMA="running"
fi
if container_running ollama; then
  ok "Ollama läuft als Docker-Container"
  FOUND_OLLAMA="docker"
fi
if [ "$FOUND_OLLAMA" = "no" ]; then
  info "Ollama nicht gefunden (optional – kann statt LM Studio als lokales Fallback verwendet werden)"
fi

# -----------------------------------------------------------------------------
header "11. Datenverzeichnisstruktur"
for d in data/market_data data/backtest_results data/signals data/orders \
         data/heartbeats data/reports data/logs; do
  mkdir -p "$d"
done
touch data/.gitkeep data/reports/.gitkeep data/logs/.gitkeep data/heartbeats/.gitkeep
ok "data/-Struktur sichergestellt"

# -----------------------------------------------------------------------------
header "12. Environment (.env)"
if [ ! -f "$REPO_ROOT/.env" ]; then
  if [ -f "$REPO_ROOT/.env.example" ]; then
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    ok ".env aus .env.example erstellt – bitte API-Keys eintragen:  nano .env"
  else
    warn ".env.example fehlt – bitte manuell anlegen"
  fi
else
  ok ".env existiert bereits"
fi

# -----------------------------------------------------------------------------
header "13. Setup-Report"
ZHF_VERSION=$(python3 -c "import sys; sys.path.insert(0,'scripts/common'); from version import __version__; print(__version__)" 2>/dev/null || echo "0.0.0")
ZHF_CODENAME=$(python3 -c "import sys; sys.path.insert(0,'scripts/common'); from version import __codename__; print(__codename__)" 2>/dev/null || echo "")
cat > data/setup_report.json <<JSON
{
  "version": "$ZHF_VERSION",
  "codename": "$ZHF_CODENAME",
  "ts": "$(date -Iseconds)",
  "cwd": "$REPO_ROOT",
  "python": {
    "version": "$(python3 --version 2>&1 | awk '{print $2}')",
    "venv": "$REPO_ROOT/.venv",
    "deps": "installed"
  },
  "node": {
    "version": "$(node --version 2>/dev/null | sed 's/^v//' || echo 'not-found')"
  },
  "opencode": {
    "found": "$FOUND_OPENCODE",
    "config_present": $( [ -f "$REPO_ROOT/.opencode/opencode.json" ] && echo true || echo false )
  },
  "paperclip": {
    "state": "$FOUND_PAPERCLIP",
    "port_3100_open": $(port_open 127.0.0.1 3100 && echo true || echo false)
  },
  "database": {
    "postgres": "$FOUND_PG",
    "note": "Paperclip nutzt embedded PostgreSQL falls kein separater Server verfügbar"
  },
  "llm": {
    "primary": "opencode zen (free-tier)",
    "lmstudio": "$FOUND_LMSTUDIO",
    "ollama": "$FOUND_OLLAMA"
  },
  "issues": [
$(if [ ${#ISSUES[@]} -gt 0 ]; then
  for i in "${ISSUES[@]}"; do printf '    "%s",\n' "$i"; done | sed '$ s/,$//'
else
  printf '    "none"\n'
fi)
  ]
}
JSON
ok "Report nach data/setup_report.json geschrieben"

# -----------------------------------------------------------------------------
header "Zusammenfassung"
if [ ${#ISSUES[@]} -eq 0 ]; then
  ok "${BOLD}Alle Voraussetzungen erfüllt.${RESET}"
else
  warn "${#ISSUES[@]} Problem(e) gefunden:"
  for i in "${ISSUES[@]}"; do
    err " - $i"
  done
fi

say ""
say "${BOLD}Nächste Schritte:${RESET}"
say "  1. Bearbeite .env und trage Broker-Keys ein (wenigstens Alpaca-Paper):  nano .env"
say "  2. Wenn OpenCode installiert ist – Test der Free-Modelle:"
say "       opencode run --model opencode/big-pickle --auto \"Sag OK\""
say "  3. Führe einen Syntax/Offline-Test durch:"
say "       python -m scripts.monitoring.synth_test"
say "       python -m scripts.monitoring.watchdog"
say "  3b. Konfig auf dieses Repo anpassen (Pfade/Paperclip-Import):"
say "       python -m scripts.sync_env"
say "       python -m scripts.materialize_config"
say "  4. Starte Paperclip:"
say "       npx paperclipai onboard --yes"
say "  5. Lege die Agenten gemäss docs/PAPERCLIP_SETUP.md an."
say ""
