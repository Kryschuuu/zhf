"""CEO – End-of-Day Tagesbericht.

Wird täglich um 22:00 NY (04:00/05:00 CET) ausgeführt. Analysiert Performance,
passt Strategie-Gewichte und Watchlist an und schreibt daily_report.md für das
Board.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.llm import call_llm, LLMError
from scripts.common.state import SharedState, Heartbeat, now_stamp
from exchanges.factory import get_exchanges

log = get_logger("ceo")


def _today_fills() -> list[dict]:
    """Fills des laufenden Tages (UTC) – via gemeinsames Schema in common.fills."""
    from scripts.common.fills import read_fills
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return read_fills(since=midnight)


def _week_performance() -> dict:
    path = cfg.DATA_DIR / "equity_log.jsonl"
    if not path.exists():
        return {}
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    eqs = []
    with path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
                ts = datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if ts >= week_start:
                    eqs.append((ts, d["equity"]))
            except Exception:
                continue
    if len(eqs) < 2:
        return {"trend": "insufficient_data"}
    first = eqs[0][1]; last = eqs[-1][1]
    peak = max(e for _, e in eqs)
    dd = (last - peak) / peak * 100 if peak else 0
    return {
        "week_start_eq": round(first, 2),
        "current_eq": round(last, 2),
        "week_return_pct": round((last - first) / first * 100, 2) if first else 0,
        "max_dd_pct": round(dd, 2),
    }


def _agent_health() -> list[dict]:
    out = []
    hb_dir = cfg.DATA_DIR / "heartbeats"
    if not hb_dir.exists():
        return out
    now = time.time()
    for name in ("ceo", "research", "backtest", "risk", "execution", "cost_optimizer"):
        hb = Heartbeat.read(name)
        if not hb.last_run:
            out.append({"agent": name, "status": "never_run", "age_min": None})
            continue
        from scripts.common.state import parse_stamp
        ts = parse_stamp(hb.last_run)
        age_min = -1 if ts is None else int((now - ts) / 60)
        out.append({"agent": name, "status": hb.status, "age_min": age_min, "last_msg": hb.message})
    return out


def _load_strategy_weights() -> dict:
    p = cfg.STRATEGIES_DIR / "strategy_weights.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"mean_reversion": 1.0, "breakout": 1.0, "ema_crossover": 1.0, "macd_flip": 0.8}


def run() -> int:
    t0 = time.time()
    log.info("=== CEO Daily Briefing start ===")
    hb = Heartbeat(agent="ceo", last_run="", status="running")
    try:
        exchanges = get_exchanges()
        portfolio = SharedState.portfolio()
        fills_today = _today_fills()
        week = _week_performance()
        health = _agent_health()

        # Aggregate PnL
        today_realized = 0.0
        for f in fills_today:
            # Sehr grob – wir haben keine Fill-PnL pro Trade direkt; wir schätzen via Portfolio-Delta
            pass
        eq = portfolio.get("equity", 0)
        cash = portfolio.get("cash", 0)
        positions = portfolio.get("positions", [])

        fee_report = {}
        fr_path = cfg.REPORTS_DIR / "fee_report.json"
        if fr_path.exists():
            try:
                fee_report = json.loads(fr_path.read_text())
            except Exception:
                pass

        # Kill-Switch Status
        ks_active, ks_reason = SharedState.killswitch_active()

        prompt_data = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "portfolio": {"equity": eq, "cash": cash, "n_positions": len(positions),
                          "positions": positions},
            "fills_today": len(fills_today),
            "fills_today_detail": fills_today[-20:],
            "week_performance": week,
            "fee_report_summary": {
                # Cost-Optimizer schreibt "fees" (24h-Fenster); "last_hour" ist das
                # alte Format – beides akzeptieren, sonst steht im Bericht "None".
                "fees_usd": (fee_report.get("fees") or {}).get("total_fees_usd",
                              (fee_report.get("last_hour") or {}).get("total_fees_usd")),
                "trades": (fee_report.get("fees") or {}).get("trades"),
                "avg_slippage_bps": (fee_report.get("fees") or {}).get("avg_slippage_bps"),
                "recommendations": fee_report.get("recommendations", []),
                "resources": fee_report.get("system_resources", {}),
            },
            "agent_health": health,
            "killswitch": {"active": ks_active, "reason": ks_reason},
            "strategy_weights": _load_strategy_weights(),
        }

        sys_prompt_path = Path(cfg.PROMPTS_DIR) / "ceo.md"
        sys_prompt = sys_prompt_path.read_text()
        user_prompt = (
            "Es ist Ende des Handelstages. Erstelle den Tagesbericht gemäss deiner "
            "Rollenbeschreibung. Die Daten für heute:\n\n"
            f"{json.dumps(prompt_data, default=str, indent=2)}\n\n"
            "Gib den Tagesbericht als MARKDOWN-Text zurück (kein JSON). Strukturiere ihn "
            "genau nach den Punkten in deiner Prompt-Definition: Executive Summary, "
            "Performance, Trades, Positions, Risk, System-Health, Empfehlungen, Plan."
        )

        try:
            resp = call_llm(user_prompt, system_prompt=sys_prompt,
                            agent="ceo", temperature=0.3, max_tokens=8000)
            report_md = resp.text
        except LLMError as e:
            log.error("CEO LLM call failed, generating fallback report: %s", e)
            report_md = _fallback_report(prompt_data)

        out_path = cfg.REPORTS_DIR / "daily_report.md"
        out_path.write_text(report_md)
        log.info("Daily report written to %s (%.1fs)", out_path, time.time() - t0)
        hb.status = "ok"
        hb.message = f"daily report: equity=${eq:.2f}, fills={len(fills_today)}"

        # Vorschlag für neue Strategie-Gewichte basierend auf einfacher Regel:
        # - Bei mehr Verlusten als Gewinnen: schwächste Strategie um 20% reduzieren
        # (Vollautomatische Anpassung; LLM-Empfehlung hat nur informativen Charakter,
        #  wir vermeiden Halluzinationen durch eine zusätzliche Regel-basierte Logik)
        _auto_adjust_weights(fills_today)

        return 0
    except Exception as e:
        log.exception("CEO daily failed: %s", e)
        hb.status = "error"; hb.message = str(e)[:200]; return 1
    finally:
        hb.last_run = now_stamp()
        hb.duration_s = time.time() - t0
        hb.write()


def _fallback_report(d: dict) -> str:
    port = d["portfolio"]
    week = d.get("week_performance", {})
    ks = d.get("killswitch", {})
    lines = [
        f"# Täglicher Bericht – {d['date']}",
        "",
        "## Executive Summary",
        f"- (Fallback-Modus: LLM nicht erreichbar). Equity: **${port['equity']:.2f}**, Cash: ${port['cash']:.2f}, {port['n_positions']} offene Positionen, {d['fills_today']} Trades heute.",
        f"- Killswitch: {'AKTIV – ' + ks.get('reason', '') if ks.get('active') else 'inaktiv'}.",
        "",
        "## Performance",
        f"- Wochenrendite: {week.get('week_return_pct', 'n/a')}%, Max-DD: {week.get('max_dd_pct', 'n/a')}%.",
        "",
        "## System-Health",
    ]
    for a in d.get("agent_health", []):
        lines.append(f"- {a['agent']}: {a['status']} (last msg: {a.get('last_msg','')})")
    lines.append("")
    lines.append("## Empfehlungen")
    lines.append("- Manuelle Prüfung empfohlen – CEO-LLM war zur Berichtszeit nicht erreichbar.")
    return "\n".join(lines)


def _auto_adjust_weights(fills: list[dict]) -> dict:
    """Heuristik: Strategie abwerten, wenn >2 Trades davon Fehler/Rejections hatten.

    Bewusst eine klare Regel und kein LLM – der Gewichts-Vorschlag des LLM ist nur
    informativ (siehe prompts/ceo.md), damit eine Halluzination nicht die Strategie-
    Gewichte im Live-Pfad verbiegt.
    """
    weights = _load_strategy_weights()
    if not fills:
        return weights
    bad: dict[str, int] = {}
    for f in fills:
        status = str(f.get("status", "")).upper()
        if status in ("REJECTED", "CANCELED") or f.get("error"):
            strat = str(f.get("strategy") or "unknown")
            bad[strat] = bad.get(strat, 0) + 1
    changed = False
    for strat, count in bad.items():
        if count > 2 and strat in weights and weights[strat] > 0.2:
            weights[strat] = round(max(0.2, weights[strat] * 0.8), 3)
            changed = True
            log.warning("CEO: Strategie '%s' abgewertet auf %.3f (%d Fehler heute)",
                        strat, weights[strat], count)
    if changed:
        p = cfg.STRATEGIES_DIR / "strategy_weights.json"
        try:
            p.write_text(json.dumps(weights, indent=2))
        except OSError as e:
            log.error("strategy_weights.json nicht schreibbar: %s", e)
    return weights


if __name__ == "__main__":
    sys.exit(run())
