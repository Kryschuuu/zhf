"""Watchdog – prüft Heartbeats, System-Ressourcen, Broker-/Marktdaten-Erreichbarkeit.
Wird alle 10 Minuten per Cron/Paperclip-Routine aufgerufen.

Korrekturen:
- Kritische Meldungen wurden per String-Matching erkannt (`"RAM9"`, `"disk"`) –
  ein Disk-Hinweis bei 85 % löste damit einen Killswitch aus, ein RAM-Problem
  nicht. Jetzt explizite Severity je Befund.
- Der Watchdog prüft neu die Marktdaten-Diagnose des Research-Agenten und die
  Adapter-Status (Feed-Rechte, Testnet-Wirkung, Circuit-Breaker) und gibt
  konkrete Reparatur-Hinweise aus.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat, parse_stamp
from exchanges.factory import get_exchanges, reset_cache, broker_diagnostics, unavailable_brokers
from scripts.common.net import health_snapshot

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

OK = "ok"
WARN = "warn"
CRITICAL = "critical"


@dataclass
class Issue:
    msg: str
    severity: str = WARN
    hint: str = ""

    def as_dict(self) -> dict:
        return {"msg": self.msg, "severity": self.severity, "hint": self.hint}


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, msg: str, severity: str = WARN, hint: str = "") -> None:
        self.issues.append(Issue(msg, severity, hint))

    def by_severity(self, sev: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == sev]


def _check_heartbeats(rep: Report, now: float) -> None:
    for agent, max_age_min in EXPECTED_INTERVALS.items():
        hb = Heartbeat.read(agent)
        if not hb.last_run:
            if agent in ("execution", "risk", "research", "backtest"):
                rep.add(f"{agent}: lief noch nie", WARN, "Cron/Heartbeat-Zeitplan prüfen")
            continue
        ts = parse_stamp(hb.last_run)
        age = 9999 if ts is None else (now - ts) / 60
        if age > max_age_min:
            rep.add(f"{agent}: Heartbeat {int(age)}min alt (erwartet <{max_age_min}min)",
                    CRITICAL if agent == "execution" and age > 3 * max_age_min else WARN,
                    "Paperclip-Agent läuft nicht / läuft zu lange")
        if hb.status == "error":
            sev = CRITICAL if agent in ("execution", "risk") else WARN
            rep.add(f"{agent}: status=error – {(hb.message or '')[:120]}", sev,
                    f"data/logs/{agent}.log ansehen")


def _check_llm(rep: Report) -> tuple[bool, bool]:
    from scripts.common.llm import _discover_local_endpoint, reset_local_cache
    reset_local_cache()
    local_ep = _discover_local_endpoint()
    opencode_ok = shutil.which(cfg.OPENCODE_BIN) is not None
    if not opencode_ok:
        rep.add(f"OpenCode CLI '{cfg.OPENCODE_BIN}' nicht in PATH", WARN,
                "Research/Risk-Kommentare laufen dann nur über lokales Fallback-Modell")
    if local_ep is None:
        rep.add("Kein lokaler LLM-Server erreichbar (LM Studio/Ollama) – nur Fallback", OK)
    if not opencode_ok and local_ep is None and not cfg.SKIP_LLM:
        rep.add("Weder OpenCode noch lokaler LLM erreichbar – Agenten antworten nicht",
                CRITICAL, "opencode installieren oder LM Studio starten; alternativ ZHF_SKIP_LLM=1")
    return opencode_ok, local_ep is not None


def _check_brokers(rep: Report) -> dict:
    reset_cache()
    exchanges = get_exchanges()
    if not exchanges:
        msg = "Kein Broker verbunden"
        if cfg.SYNTH:
            rep.add(f"{msg} (Synth-Modus aktiv!)", CRITICAL, "ZHF_SYNTH/DATA_DIR prüfen")
        elif cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
            rep.add(f"{msg} – API-Keys in .env eintragen (im Setup normal)", OK if cfg.ENVIRONMENT == "paper" else WARN)
        else:
            rep.add(f"{msg} – API-Keys/Netz prüfen", CRITICAL)
        return {}
    for name, ex in exchanges.items():
        acc = ex.get_account()
        if acc.get("error"):
            rep.add(f"{name}: Konto nicht lesbar ({str(acc['error'])[:100]})", WARN,
                    "Keys/Rechte/Endpoint prüfen")
        if float(acc.get("equity") or 0) <= 0:
            rep.add(f"{name}: equity=0", CRITICAL, "Ohne Equity rechnet Risk falsche Positionsgrössen")
        st = ex.status() or {}
        if st.get("rejected_feeds"):
            rep.add(f"alpaca: Data-Feed(s) {st['rejected_feeds']} ohne Rechte", CRITICAL,
                    "ALPACA_DATA_FEED=iex (Free-Plan) oder Abo buchen")
        if st.get("testnet_requested") and st.get("testnet_active") is False:
            rep.add(f"{name}: TESTNET=true, aber Sandbox NICHT aktiv", CRITICAL,
                    "ccxt-Adapter aktualisieren oder auf Production mit DRY_RUN=true umstellen")
        if st.get("read_only"):
            rep.add(f"{name}: read-only Modus (nur Marktdaten)", OK,
                    "Echte Orders erst mit BITUNIX_ALLOW_LIVE_ORDERS=true + DRY_RUN=false")
        br = (st.get("breaker") or "")
        if "Fehler in Folge" in br:
            rep.add(f"{name}: {br}", WARN, "kurze Pause durch Circuit-Breaker – Endpoint/Netz prüfen")
    return exchanges


def _check_market_data(rep: Report) -> None:
    md = SharedState.market_data_status()
    if not md:
        return
    ok = md.get("assets_ok") or 0
    total = md.get("assets") or 0
    if total and ok == 0:
        rep.add(f"Marktdaten komplett leer (0/{total} Assets nutzbar)", CRITICAL,
                f"Detail: {list((md.get('errors') or {}).items())[:2]}")
    elif total and ok / total < 0.6:
        rep.add(f"Marktdaten lückenhaft ({ok}/{total} Assets)", WARN,
                "Feed-Rechte (ALPACA_DATA_FEED), Watchlist-Symbole und Broker-Status prüfen")
    for hb_name in ("research",):
        hb = Heartbeat.read(hb_name)
        if hb.status == "error" and "no market data" in (hb.message or ""):
            rep.add("Research meldet 'no market data' – keine Signale möglich", CRITICAL,
                    "data/market_data/status.json lesen")


def _check_resources(rep: Report) -> dict:
    try:
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage(str(cfg.DATA_DIR)).percent
        cpu = psutil.cpu_percent(interval=0.3)
    except Exception as e:  # noqa: BLE001
        rep.add(f"Ressourcenmessung fehlgeschlagen: {e}", OK)
        ram = disk = cpu = -1
    if ram > 90:
        rep.add(f"RAM {ram:.0f}% – OOM-Risiko", CRITICAL, "weniger parallele Agenten/kleinere Modelle")
    elif ram > 80:
        rep.add(f"RAM {ram:.0f}%", WARN)
    if disk > 90:
        rep.add(f"Disk {disk:.0f}% voll", CRITICAL, "data/logs und data/backtest_results aufräumen")
    elif disk > 80:
        rep.add(f"Disk {disk:.0f}%", WARN)
    if cpu > 95:
        rep.add(f"CPU {cpu:.0f}% dauerhaft hoch – Herzschlag-Zyklen laufen nach", WARN)
    return {"ram_pct": round(ram, 1), "disk_pct": round(disk, 1), "cpu_pct": round(cpu, 1)}


def _check_orders(rep: Report) -> None:
    state = SharedState.order_state()
    open_orders = state.get("orders", [])
    stale = [o for o in open_orders
             if (datetime.now(timezone.utc) - datetime.fromisoformat(o.get("submitted_at",
                                                                            "2000-01-01T00:00:00+00:00"))
                 ).total_seconds() > 3600]
    if stale:
        rep.add(f"{len(stale)} Order(s) > 1h offen", WARN, "Order-Status am Broker prüfen / stornieren")
    pending = SharedState.approved()
    if pending:
        try:
            age = time.time() - (cfg.DATA_DIR / "orders" / "approved.json").stat().st_mtime
        except OSError:
            age = 0
        if age > 900:
            rep.add(f"{len(pending)} freigegebene Trade(s) warten {int(age // 60)}min auf Execution",
                    CRITICAL, "Execution-Agent läuft nicht an (Cron/Heartbeat)")


def run() -> int:
    log.info("=== Watchdog start ===")
    now = time.time()
    rep = Report()

    _check_heartbeats(rep, now)
    opencode_ok, local_llm_ok = _check_llm(rep)
    exchanges = _check_brokers(rep)
    _check_market_data(rep)
    resources = _check_resources(rep)
    _check_orders(rep)

    crit = rep.by_severity(CRITICAL)
    if crit:
        SharedState.activate_killswitch("watchdog: " + "; ".join(i.msg for i in crit)[:200])
        log.error("Watchdog: %d KRITISCHE Befunde → Killswitch: %s", len(crit),
                  " | ".join(i.msg for i in crit)[:300])
    elif rep.issues:
        log.warning("Watchdog: %d Hinweise – %s", len(rep.issues),
                    "; ".join(f"[{i.severity}] {i.msg}" for i in rep.issues[:6]))
    else:
        log.info("Watchdog: all green.")

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": not crit,
        "issues": [i.as_dict() for i in rep.issues],
        "critical_count": len(crit),
        "config": cfg.redacted(),
        "resources": resources,
        "n_exchanges": len(exchanges),
        "brokers": broker_diagnostics(),
        "unavailable_brokers": unavailable_brokers(),
        "net_health": health_snapshot(),
        "opencode_cli_ok": opencode_ok,
        "local_llm": local_llm_ok,
        "killswitch": SharedState.killswitch_active()[1],
    }
    try:
        out = cfg.REPORTS_DIR / "watchdog.json"
        out.write_text(json.dumps(report, indent=2, default=str))
    except OSError as e:
        log.error("watchdog.json nicht schreibbar: %s", e)
    return 0 if not crit else 1


def deactivate_killswitch(reason: str = "manual reset after fixing root cause") -> None:
    """Nach behobener Ursache: Killswitch wieder aus (sonst bleibt die Anlage stehen)."""
    SharedState.deactivate_killswitch()
    log.warning("Killswitch deaktiviert (%s) – Handel läuft wieder.", reason)


if __name__ == "__main__":
    if "--reset-killswitch" in sys.argv:
        deactivate_killswitch()
        sys.exit(0)
    sys.exit(run())
