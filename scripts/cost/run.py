"""Cost Optimizer Agent – stündlich.

Analysiert Gebühren, Slippage, Systemressourcen; schreibt fee_report.json
mit Empfehlungen.

Korrekturen:
- Gebühren basieren jetzt auf der realen Fee-Schedule pro Broker/Markt
  (`scripts/common.fees`) statt "Alpaca 0 %, sonst pauschal 0.1 %".
- Slippage war hartkodiert 0.0 ("unbekannt ohne Referenz") – der Execution
  Agent schreibt jetzt Referenzpreis und Ausführungspreis ins Fills-Log,
  hier wird die echte Abweichung ausgewertet.
- Kein LLM-Kommentar mehr, wenn gar keine Trades vorliegen (sonst halluziniert
  das Modell Kostenprobleme, die es nicht gibt).
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import psutil

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat, now_stamp
from scripts.common.fees import breakeven_move_pct, expected_costs, fees
from scripts.common.fills import read_fills
from scripts.common.llm import call_llm, LLMError

log = get_logger("cost")

WINDOW_HOURS = 24
FILLS_LOG = cfg.LOG_DIR / "fills.log"


def _parse_fills(since: datetime) -> list[dict]:
    """Fills im Zeitfenster – Schema/Fallbacks in scripts.common.fills."""
    return read_fills(since=since)


def _f(row: dict, key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key)
        return float(v) if v not in (None, "", "nan") else default
    except (TypeError, ValueError):
        return default


def _aggregate(fills: list[dict]) -> dict:
    by_broker: dict[str, dict] = defaultdict(lambda: {"trades": 0, "notional": 0.0,
                                                      "fees": 0.0, "slippage_usd": 0.0,
                                                      "slip_bps": []})
    total_notional = 0.0
    total_fees = 0.0
    slips: list[float] = []
    for r in fills:
        broker = str(r.get("broker") or "unknown")
        market = str(r.get("market") or ("crypto_perp" if "USDT" in str(r.get("symbol", "")) else "stocks"))
        notional = _f(r, "notional_usd")
        fee = _f(r, "est_fee_usd")
        if not fee and notional:  # altes Fills-Format ohne est_fee_usd
            fee = notional * fees(broker, market).round_trip_taker
        taker = fees(broker, market).taker
        slip_bps = _f(r, "slippage_bps")
        entry = by_broker[broker]
        entry["trades"] += 1
        entry["notional"] += notional
        entry["fees"] += fee
        if notional and slip_bps:
            entry["slippage_usd"] += abs(notional * slip_bps / 10_000)
            entry["slip_bps"].append(slip_bps)
            slips.append(slip_bps)
        elif notional and not _f(r, "ref_price"):
            # ohne Referenzpreis: mindestens der Spread, den wir zahlen mussten
            entry["slippage_usd"] += notional * taker
        total_notional += notional
        total_fees += fee

    out = {"trades": len(fills), "total_notional_usd": round(total_notional, 4),
           "total_fees_usd": round(total_fees, 4),
           "avg_fee_bps": round(total_fees / total_notional * 10_000, 2) if total_notional else 0.0,
           "avg_slippage_bps": round(sum(slips) / len(slips), 2) if slips else 0.0,
           "max_abs_slippage_bps": round(max((abs(s) for s in slips), default=0.0), 2),
           "slippage_measured": len(slips),
           "by_broker": {k: {"trades": v["trades"], "notional_usd": round(v["notional"], 2),
                             "fees_usd": round(v["fees"], 4),
                             "slippage_usd": round(v["slippage_usd"], 4),
                             "avg_slip_bps": round(sum(v["slip_bps"]) / len(v["slip_bps"]), 2)
                             if v["slip_bps"] else 0.0,
                             "fee_pct_of_notional": round(v["fees"] / v["notional"] * 100, 4)
                             if v["notional"] else 0.0}
                         for k, v in by_broker.items()}}
    out["total_cost_usd"] = round(total_fees + sum(v["slippage_usd"] for v in by_broker.values()), 4)
    return out


def _recommendations(costs: dict, resources: dict, fills: list[dict]) -> list[dict]:
    recs: list[dict] = []
    if resources["disk_pct"] > 80:
        recs.append({"type": "warning", "severity": "high" if resources["disk_pct"] > 90 else "medium",
                     "msg": f"Disk {resources['disk_pct']}% – alte Logs/Backtest-Results aufräumen"})
    if resources["ram_pct"] > 85:
        recs.append({"type": "warning", "severity": "high",
                     "msg": f"RAM {resources['ram_pct']}% – kleinere Modelle / weniger parallele Agenten"})
    n = costs["trades"]
    if n == 0:
        recs.append({"type": "info", "severity": "low",
                     "msg": f"Keine Fills in den letzten {WINDOW_HOURS}h – Kostenrechnung ist "
                            f"leer. Prüfe die Marktdaten (data/market_data/status.json)."})
        return recs
    if costs["avg_fee_bps"] > 10:
        recs.append({"type": "warning", "severity": "medium",
                     "msg": f"Ø Gebühren {costs['avg_fee_bps']} bps/Trade – Tradingfrequenz senken "
                            f"oder Maker-Orders (Limit) nutzen"})
    if costs["avg_slippage_bps"] > 15:
        recs.append({"type": "warning", "severity": "medium",
                     "msg": f"Ø Slippage {costs['avg_slippage_bps']} bps – zu grosse Orders oder "
                            f"dünne Liquidität; in Liquiditätsfenstern handeln"})
    for broker, c in costs["by_broker"].items():
        if c["fee_pct_of_notional"] > 0.15:
            recs.append({"type": "warning", "severity": "medium",
                         "msg": f"{broker}: Gebühren {c['fee_pct_of_notional']}% des Notionals – "
                                f"kleinere/entsprechend grössere Trades bündeln"})
    used = {(str(r.get("broker")),
             "crypto_perp" if "USDT" in str(r.get("symbol", "")) else
             ("crypto" if "/" in str(r.get("symbol", "")) else "stocks")) for r in fills}
    be = {f"{b}/{m}": breakeven_move_pct(b, m) for b, m in used if breakeven_move_pct(b, m) > 0}
    recs.append({"type": "info", "severity": "low",
                 "msg": "Break-even-Bewegung (Entry+Exit): "
                        + ", ".join(f"{k} {v}%" for k, v in be.items() if v)})
    return recs


def run() -> int:
    t0 = time.time()
    log.info("=== Cost Optimizer start ===")
    hb = Heartbeat(agent="cost_optimizer", last_run="", status="running")
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
        fills = _parse_fills(since)
        costs = _aggregate(fills)
        try:
            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage(str(cfg.DATA_DIR)).percent
        except Exception as e:  # noqa: BLE001  (psutil schlägt in Containern auf)
            log.debug("Ressourcenmessung fehlgeschlagen: %s", e)
            cpu = ram = disk = -1.0
        resources = {"cpu_pct": round(cpu, 1), "ram_pct": round(ram, 1), "disk_pct": round(disk, 1),
                     "load_1m": round(psutil.getloadavg()[0], 2) if hasattr(psutil, "getloadavg") else None}
        recs = _recommendations(costs, resources, fills)

        md_status = SharedState.market_data_status()
        if md_status and md_status.get("state") != "ok":
            recs.append({"type": "warning", "severity": "high",
                         "msg": f"Marktdaten-lückenhaft ({md_status.get('assets_ok')}/{md_status.get('assets')} "
                                f"Assets ok) – ohne Daten keine Edge-Deckung der Fixkosten"})

        commentary = ""
        if costs["trades"] > 0:
            try:
                sys_prompt = ("Du bist Betriebswirtschaftler eines quanten Trading-Teams. Gib 1-2 kurze, "
                              "präzise Empfehlungen zur Kosteneffizienz auf Basis der Zahlen. Antworte als "
                              "JSON: {\"commentary\": str, \"recommendations\": [str]}")
                payload = {"window_hours": WINDOW_HOURS, "costs": costs, "resources": resources}
                resp = call_llm(json.dumps(payload, default=str), system_prompt=sys_prompt,
                                agent="cost", temperature=0.2, max_tokens=700)
                try:
                    d = json.loads(resp.text)
                    commentary = str(d.get("commentary", ""))[:500]
                    for r in d.get("recommendations", [])[:5]:
                        recs.append({"type": "tip", "severity": "low", "msg": str(r)[:200]})
                except (json.JSONDecodeError, TypeError):
                    commentary = resp.text[:300]
            except LLMError as e:
                log.info("LLM comment unavailable: %s", str(e)[:150])

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": WINDOW_HOURS,
            "mode": "dry_run" if cfg.DRY_RUN else ("synth" if cfg.SYNTH else "live"),
            "fees": costs,
            "fee_schedule": {b: {m: fees(b, m).as_dict() for m in
                                 {"stocks", "crypto", "crypto_perp"}} for b in
                             ("alpaca", "bingx", "bitunix")},
            "system_resources": resources,
            "market_data": {k: v for k, v in (md_status or {}).items() if k != "brokers"},
            "recommendations": recs,
            "commentary": commentary,
        }
        out = cfg.REPORTS_DIR / "fee_report.json"
        try:
            out.write_text(json.dumps(report, indent=2, default=str))
        except OSError as e:
            log.error("fee_report.json nicht schreibbar: %s", e)
        log.info("Cost report: fees=$%.2f + slippage=$%.2f (%d trades/%dh), RAM=%s%%, CPU=%s%%",
                 costs["total_fees_usd"], sum(v["slippage_usd"] for v in costs["by_broker"].values()),
                 costs["trades"], WINDOW_HOURS, resources["ram_pct"], resources["cpu_pct"])
        hb.status = "ok"
        hb.message = (f"fees=${costs['total_fees_usd']:.2f} trades={costs['trades']} "
                      f"slip={costs['avg_slippage_bps']}bps ram={resources['ram_pct']}%")
        hb.details = {"recommendations": len(recs), "window_hours": WINDOW_HOURS}
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("Cost optimizer failed: %s", e)
        hb.status, hb.message = "error", str(e)[:200]
        return 1
    finally:
        hb.last_run = now_stamp()
        hb.duration_s = time.time() - t0
        hb.write()


if __name__ == "__main__":
    sys.exit(run())
