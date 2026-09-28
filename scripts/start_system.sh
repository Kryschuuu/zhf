#!/usr/bin/env bash
# Startet alle benötigten Hintergrund-Komponenten.
# Nur für Entwicklung/Test; im Produktivbetrieb wird das von Paperclip gesteuert.
set -e
cd "$(dirname "$0")/.."

echo "=== ZHF Trading System Starter ==="
echo "Stelle sicher, dass LM Studio läuft auf Port 1234 mit dem 3B-Modell!"
echo ""

source .venv/bin/activate

# Watchdog im Hintergrund alle 10 Minuten
while true; do
  python -m scripts.monitoring.watchdog || true
  sleep 600
done &
WATCHDOG_PID=$!

# Pipeline-Loop (vereinfacht – im Betrieb nutze Paperclip-Routinen)
# Achtung: Dieser Loop triggert Orders aus approved.json wenn Risk etwas freigibt.
# Nutze DRY_RUN=true im .env zum Testen.
echo "Watchdog läuft als PID $WATCHDOG_PID"
echo "Starte Pipeline-Zyklus alle 5 Minuten (zum Testen)."
trap "kill $WATCHDOG_PID 2>/dev/null; exit" INT TERM
while true; do
  python -m scripts.run_pipeline || true
  sleep 300
done
