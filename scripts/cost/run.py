"""Cost Optimizer Agent – stündlich.

Analysiert Gebühren, Slippage, Systemressourcen; schreibt fee_report.json
mit Empfehlungen.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat
from scripts.common.llm import call_llm_json, LLMError

log = get_logger("cost")


def _parse_fills_last_hour() -> list[dict]:
    path = cfg.LOG_DIR / "fills.log"
    if not path.exists():
        return []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    fills = []
    try:
        with path.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        fills.append(row)
                except Exception:
                    continue
    except Exception as e:
        log.warning("Could not read fills log: %s", e)
    return fills


def run() -> int:
    t0 = time.time()
    log.info("=== Cost Optimizer start ===")
    hb = Heartbeat(agent="cost_optimizer", last_run="", status="running")
    try:
        fills = _parse_fills_last_hour()

        # Grobe Gebühren/Slippage-Schätzung
        # (Für echte Werte müssten wir Broker-Fee-Endpoints abfragen – dies ist eine
        #  Indikation basierend auf bekannten Fee-Schedules.)
        fees_by_broker: dict[str, float] = {}
        n = len(fills)
        total_notional = 0.0
        total_fees = 0.0
        slippages: list[float] = []
        for r in fills:
            broker = r["broker"]
            notional = float(r.get("notional_usd") or 0)
            total_notional += notional
            fee_rate = 0.0 if broker == "alpaca" else 0.001  # Alpaca Aktien kommissionsfrei
            fee = notional * fee_rate
            fees_by_broker[broker] = fees_by_broker.get(broker, 0) + fee
            total_fees += fee
            # Slippage ist unbekannt ohne Referenz; wir markieren N/A
            slippages.append(0.0)

        # System-Ressourcen
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent

        # Empfehlungen generieren (Regelbasiert + kleines LLM für Kommentare)
        recs: list[dict] = []
        if disk > 80:
            recs.append({"type": "warning", "severity": "high", "msg": f"Disk usage {disk}%"})
        if ram > 85:
            recs.append({"type": "warning", "severity": "high",
                         "msg": f"RAM usage {ram}% – reduce model size or stop parallel agents"})
        if n > 0:
            fee_pct = (total_fees / total_notional * 100) if total_notional > 0 else 0
            if fee_pct > 0.15:
                recs.append({"type": "warning", "severity": "medium",
                             "msg": f"Fees last hour {fee_pct:.3f}% of notional – reduce trade frequency"})

        # LLM-Kommentar (optional, nur wenn ein Modell verfügbar ist)
        commentary = ""
        try:
            from scripts.common.llm import call_llm
            sys_prompt = "Du bist Betriebswirtschaftler. Gib 1-2 kurze, präzise Empfehlungen zur Kosteneffizienz auf Basis der unten stehenden Zahlen. Antworte als JSON: {\"commentary\": str, \"recommendations\": [str]}"
            payload = {
                "last_hour": {"trades": n, "total_fees_usd": round(total_fees, 2),
                              "fees_by_broker": fees_by_broker, "notional_usd": round(total_notional, 2)},
                "resources": {"cpu_pct": cpu, "ram_pct": ram, "disk_pct": disk},
            }
            resp = call_llm(json.dumps(payload), system_prompt=sys_prompt,
                            agent="cost", temperature=0.2, max_tokens=800)
            try:
                d = json.loads(resp.text)
                commentary = d.get("commentary", "")
                for r in d.get("recommendations", []):
                    recs.append({"type": "tip", "severity": "low", "msg": r})
            except Exception:
                commentary = resp.text[:300]
        except LLMError as e:
            log.info("LLM comment unavailable: %s", e)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "last_hour": {
                "trades": n,
                "total_fees_usd": round(total_fees, 2),
                "total_notional_usd": round(total_notional, 2),
                "avg_fee_bps": round((total_fees / total_notional * 10000), 2) if total_notional > 0 else 0,
                "fees_by_broker": {k: round(v, 2) for k, v in fees_by_broker.items()},
            },
            "system_resources": {"cpu_pct": cpu, "ram_pct": ram, "disk_pct": disk},
            "recommendations": recs,
            "commentary": commentary,
        }
        (cfg.REPORTS_DIR / "fee_report.json").write_text(json.dumps(report, indent=2))
        log.info("Cost report: fees=$%.2f (%d trades), RAM=%d%%, CPU=%d%%",
                 total_fees, n, ram, cpu)
        hb.status = "ok"
        hb.message = f"fees=${total_fees:.2f} trades={n} ram={ram}%"
        return 0
    except Exception as e:
        log.exception("Cost optimizer failed: %s", e)
        hb.status = "error"; hb.message = str(e)[:200]; return 1
    finally:
        hb.last_run = time.strftime("%Y-%m-%dT%H:%M:%S")
        hb.duration_s = time.time() - t0
        hb.write()


if __name__ == "__main__":
    sys.exit(run())
