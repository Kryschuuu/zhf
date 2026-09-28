"""Backtest Agent – läuft als Paperclip-Heartbeat (prozess-basiert).

Liest Kandidaten aus `data/signals/candidates.json`, validiert jeden via
Backtest-Engine und schreibt erfolgreiche nach `data/signals/validated.json`.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat, now_stamp
from exchanges.factory import get_exchanges
from scripts.backtest.engine import validate_candidate, save_result
from scripts.common.fees import taker_fee
from exchanges.symbols import is_valid_symbol_for_broker, normalize_symbol

log = get_logger("backtest")


def run() -> int:
    t0 = time.time()
    log.info("=== Backtest Agent start ===")
    hb = Heartbeat(agent="backtest", last_run="", status="running")
    try:
        if SharedState.killswitch_active()[0]:
            hb.status = "idle"
            hb.message = "killswitch active"
            hb.write()
            return 0

        cands = SharedState.candidates()
        if not cands:
            log.info("No candidates to validate.")
            SharedState.set_validated([])
            hb.status = "ok"
            hb.message = "no candidates"
            hb.write()
            return 0

        exchanges = get_exchanges()
        validated = []
        rejected = []
        for c in cands:
            broker = str(c.get("broker", "alpaca")).lower()
            sym = normalize_symbol(str(c.get("symbol", "")), broker)
            market = str(c.get("market") or ("crypto_perp" if "USDT" in sym else "stocks"))
            tf = c.get("timeframe", "1h")
            c = {**c, "symbol": sym, "broker": broker, "market": market}
            if not is_valid_symbol_for_broker(sym, market, broker):
                rejected.append({**c, "reason": f"invalid symbol '{sym}'"})
                continue
            ex = exchanges.get(broker)
            if not ex:
                rejected.append({**c, "reason": f"broker {broker} not available"})
                continue
            try:
                bars = ex.get_bars(sym, tf, limit=300)
            except Exception as e:
                rejected.append({**c, "reason": f"bars failed: {e}"})
                continue
            if len(bars) < 50:
                rejected.append({**c, "reason": f"only {len(bars)} bars"})
                continue
            # Realistische Taker-Gebühr pro Seite statt Pauschale (Alpaca Krypto
            # z.B. 0.25 %, BingX-Perp 0.05 %) – sonst werden Cost-Edge-Kandidaten
            # durch falsche Fees verwässert.
            fee = taker_fee(broker, market)
            res = validate_candidate(c, bars, fee_pct=fee)
            if not res:
                rejected.append({**c, "reason": "unknown strategy or insufficient data"})
                continue
            save_result(res)
            if res.get("validation_passed"):
                bt = res["backtest"]
                # Positionsgrössen-Hinweis: konservatives Half-Kelly
                wr = bt["win_rate"]; pf = bt["profit_factor"]
                if wr > 0 and pf > 1:
                    kelly = wr - (1 - wr) / (pf - 1 + 0.001)  # rough kelly for b=pf-1
                    size_pct = max(0.2, min(cfg.MAX_POSITION_SIZE_PCT, kelly * 50))  # half-kelly scaled
                else:
                    size_pct = 0.5
                validated.append({
                    **c,
                    "backtest": bt,
                    "position_size_pct_hint": round(size_pct, 2),
                    "review": res["backtest"].get("reason", "passed"),
            "validated_at": datetime.now(timezone.utc).isoformat(),
                })
            else:
                rejected.append({**c, "reason": res["backtest"].get("reason", "failed thresholds")})

        SharedState.set_validated(validated)
        log.info("Validated %d / %d candidates", len(validated), len(cands))

        # Optional: LLM für den Review-Kommentar (kleines Modell, aber robust ohne)
        # Wir lassen das LLM optional laufen – falls LM Studio nicht erreichbar ist,
        # bleibt die Review rein numerisch (sicherer).
        try:
            from scripts.common.llm import call_llm
            sys_prompt = "Du bist ein erfahrener quant. Bewerte die Backtest-Resultate kurz in einem Satz pro Trade: Achte auf Overfitting, Marktphase, Stichprobengrösse. Antworte als JSON-Array {\"reviews\": [{\"symbol\":..., \"review\":...}]}."
            if validated:
                user_payload = [{"symbol": v["symbol"], "strategy": v["strategy"], **v["backtest"]} for v in validated[:5]]
                resp = call_llm(json.dumps(user_payload), system_prompt=sys_prompt,
                                agent="backtest", temperature=0.2, max_tokens=800)
                try:
                    d = json.loads(resp.text)
                    for rev in d.get("reviews", []):
                        for v in validated:
                            if v["symbol"] == rev.get("symbol"):
                                v["review"] = rev.get("review", v["review"])
                except Exception:
                    pass
        except Exception as e:
            log.info("LLM review skipped: %s", e)

        hb.status = "ok"
        hb.message = f"validated={len(validated)} rejected={len(rejected)}"
        return 0
    except Exception as e:
        log.exception("Backtest failed: %s", e)
        hb.status = "error"
        hb.message = str(e)[:200]
        return 1
    finally:
        hb.last_run = now_stamp()
        hb.duration_s = time.time() - t0
        hb.write()


if __name__ == "__main__":
    sys.exit(run())
