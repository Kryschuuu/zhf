"""Watchdog – prüft Heartbeats, System-Ressourcen, Broker-Erreichbarkeit.
Wird alle 10 Minuten per Cron/Paperclip-Routine aufgerufen.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat
from exchanges.factory import get_exchanges, reset_cache

log = get_logger("watchdog")

# Alters-Schwellen pro Agent in Minuten
EXPECTED_INTERVALS = {
    "research": 45,
    "backtest": 30,
    "risk": 20,
    "execution": 5,
    "cost_optimizer": 75,
    "ceo": 60 * 25,  # 1x pro Tag, also 25h Toleranz
}


def run() -> int:
    log.info("=== Watchdog start ===")
    now = time.time()
    issues: list[str] = []

    # Heartbeat-Checks
    for agent, max_age_min in EXPECTED_INTERVALS.items():
        hb = Heartbeat.read(agent)
        if not hb.last_run:
            if agent in ("execution", "risk", "research", "backtest"):
                issues.append(f"{agent}: hat nie gelaufen")
            continue
        try:
            ts = time.mktime(time.strptime(hb.last_run, "%Y-%m-%dT%H:%M:%S"))
            age = (now - ts) / 60
        except Exception:
            age = 9999
        if age > max_age_min:
            issues.append(f"{agent}: heartbeat {int(age)}min alt (erwartet <{max_age_min})")
        if hb.status == "error":
            issues.append(f"{agent}: status=error – {hb.message[:120]}")

    # System-Ressourcen
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage(cfg.DATA_DIR).percent
    if ram > 90:
        issues.append(f"RAM {ram}% – System läuft Gefahr zu oomen")
    if disk > 90:
        issues.append(f"Disk {disk}% – aufräumen")

    # Lokaler LLM-Server (LM Studio/Ollama/…) – nur Info, nicht kritisch
    import shutil
    from scripts.common.llm import _discover_local_endpoint, reset_local_cache
    reset_local_cache()
    local_ep = _discover_local_endpoint()
    local_llm_ok = local_ep is not None
    local_llm_name = local_ep[0] if local_ep else "none"

    # OpenCode CLI auf Free-Tier erreichbar?
    oc_bin = shutil.which(cfg.OPENCODE_BIN)
    opencode_ok = oc_bin is not None
    if not opencode_ok:
        issues.append(f"OpenCode CLI '{cfg.OPENCODE_BIN}' nicht in PATH – primärer LLM-Harness fehlt")
    if not (opencode_ok or local_llm_ok):
        issues.append("Weder OpenCode noch ein lokaler LLM-Server erreichbar – Agenten werden keine Antworten liefern")

    # Broker stichprobenartig (ein Account-Call pro Broker)
    reset_cache()
    exchanges = get_exchanges()
    if not exchanges:
        # Keine Broker ist im Setup/Paper-Trading-Start normal – nur warnen,
        # nicht als kritisch betrachten solange DRY_RUN aktiv ist.
        if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
            issues.append("Keine Broker verbunden – für Produktivbetrieb API-Keys in .env eintragen (im DRY_RUN-Modus normal)")
        else:
            issues.append("Keine Broker verbunden – prüfe API-Keys")
    else:
        for name, ex in exchanges.items():
            try:
                acc = ex.get_account()
                if acc.get("error"):
                    issues.append(f"{name}: {acc['error']}")
            except Exception as e:
                issues.append(f"{name} get_account error: {e}")

    # Report
    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": len(issues) == 0,
        "issues": issues,
        "ram_pct": ram,
        "disk_pct": disk,
        "n_exchanges": len(exchanges),
        "opencode_cli_ok": opencode_ok,
        "local_llm": local_llm_name,
    }
    out = cfg.REPORTS_DIR / "watchdog.json"
    out.write_text(json.dumps(report, indent=2))

    # Killswitch nur bei echten Krisen auslösen – fehlende API-Keys oder
    # fehlende CLI-Tools im Setup sind keine Handels-Krisen.
    critical_keywords = ("KILLSWITCH", "RAM9", "disk",
                         "daily drawdown", "weekly drawdown")
    critical = []
    for i in issues:
        il = i.lower()
        if any(k.lower() in il for k in critical_keywords):
            critical.append(i)
        # Execution-Status=error nur dann kritisch wenn Broker konfiguriert sind
        # (sonst ist es Setup-Zustand, noch keine Orders)
        if "execution: status=error" in il and len(exchanges) > 0:
            critical.append(i)
    if critical:
        SharedState.activate_killswitch("watchdog: " + "; ".join(critical[:2]))

    if issues:
        log.warning("Watchdog found %d issues: %s", len(issues), issues)
    else:
        log.info("Watchdog: all green.")
    return 0 if not critical else 1


if __name__ == "__main__":
    sys.exit(run())
