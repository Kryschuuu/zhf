"""Research Agent Worker.

Wird vom Paperclip-Heartbeat oder manuell aufgerufen:
  python -m scripts.research.run

Lädt Watchlist, holt Bars für alle Assets, berechnet Indikatoren, gibt die
kompakte Feature-Matrix an das LLM (primär OpenCode big-pickle, Fallback
lokal) weiter und speichert Kandidaten.

Grundsatz: **keine Daten → keine Signale.** Das LLM wird nur befragt, wenn
tatsächlich Marktdaten vorliegen, und seine Ausgabe wird gegen die reale
Watchlist validiert (Anti-Halluzinations-Gate).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# Pfad-Konfiguration für Script-Aufruf
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.llm import call_llm_json, LLMError
from scripts.common.state import SharedState, Heartbeat, now_stamp
from exchanges.factory import get_exchanges, unavailable_brokers
from exchanges.symbols import is_valid_symbol_for_broker, normalize_symbol
from strategies.signals import bars_to_df, compute_indicators, generate_signals

log = get_logger("research")

MIN_BARS = 40          # unterhalb davon ist RSI14/MACD/EMA21 nicht aussagekräftig
MAX_LLM_CANDIDATES = 5
MIN_CONFIDENCE = 0.50
MAX_SL_PCT = 2.0       # aus prompts/research_agent.md

DEFAULT_WATCHLIST = [
    # Aktien (Alpaca, Commission-free, fraktional)
    {"symbol": "SPY",  "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    {"symbol": "QQQ",  "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    {"symbol": "AAPL", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    {"symbol": "MSFT", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    {"symbol": "NVDA", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    {"symbol": "TSLA", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    # Krypto-Spot (Alpaca Crypto)
    {"symbol": "BTC/USD", "broker": "alpaca", "market": "crypto", "timeframe": "1h"},
    {"symbol": "ETH/USD", "broker": "alpaca", "market": "crypto", "timeframe": "1h"},
]

# Für ZHF_SYNTH=1: dieselbe Pipeline, nur gegen den Offline-Broker.
SYNTH_WATCHLIST = [
    {"symbol": "SYNTH_A", "broker": "synth", "market": "stocks", "timeframe": "1h"},
    {"symbol": "SYNTH_B", "broker": "synth", "market": "stocks", "timeframe": "1h"},
    {"symbol": "SYNTH_ETH", "broker": "synth", "market": "crypto", "timeframe": "1h"},
]

# BingX/Bitunix Perps (werden nur hinzugefügt wenn der Broker geladen ist)
PERP_WATCHLIST = [
    {"symbol": "BTC/USDT",  "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "ETH/USDT",  "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "SOL/USDT",  "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "DOGE/USDT", "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "BTC/USDT",  "broker": "bitunix", "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "ETH/USDT",  "broker": "bitunix", "market": "crypto_perp", "timeframe": "1h"},
]


def load_watchlist(exchanges: dict) -> tuple[list[dict], list[dict]]:
    """Liest strategies/watchlist.json oder nutzt die Defaults.

    Returns (watchlist, skipped) – skipped enthält jeden Eintrag mit Grund,
    damit der Watchdog "0 Assets" von "Watchlist kaputt" unterscheiden kann.
    """
    raw: list[dict] = []
    p = Path(cfg.STRATEGIES_DIR) / "watchlist.json"
    if p.exists():
        try:
            with p.open() as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("watchlist", [])
            if isinstance(data, list) and data:
                raw = [d for d in data if isinstance(d, dict)]
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load watchlist (%s), using defaults", e)
    if not raw:
        if cfg.SYNTH:
            raw = [dict(a, broker="synth", market="stocks") for a in SYNTH_WATCHLIST]
        else:
            raw = list(DEFAULT_WATCHLIST) + [a for a in PERP_WATCHLIST if a["broker"] in exchanges]

    out: list[dict] = []
    skipped: list[dict] = []
    seen: set[tuple] = set()
    unavail = unavailable_brokers()
    for entry in raw:
        a = dict(entry)
        broker = str(a.get("broker", "alpaca")).lower()
        if cfg.SYNTH and broker != "synth":
            # Synth-Modus: jede Watchlist läuft 1:1 gegen den Fake-Broker – so
            # testet man die Pipeline inkl. eigener Watchlist, ohne Keys/Netz.
            a.setdefault("_orig_broker", broker)
            broker = "synth"
        sym = normalize_symbol(str(a.get("symbol", "")), broker)
        market = str(a.get("market") or ("crypto_perp" if "USDT" in sym and "/" in sym else
                                        "crypto" if "/" in sym else "stocks"))
        a.update({"symbol": sym, "broker": broker, "market": market,
                  "timeframe": str(a.get("timeframe") or "1h")})
        key = (broker, sym, a["timeframe"])
        if not sym:
            skipped.append({**a, "reason": "leeres Symbol"})
            continue
        if key in seen:
            skipped.append({**a, "reason": "Duplikat"})
            continue
        seen.add(key)
        if broker not in exchanges:
            skipped.append({**a, "reason": f"broker '{broker}' nicht geladen"
                                           f" ({unavail.get(broker, 'keine Keys/Keys fehlen')})"})
            continue
        if not is_valid_symbol_for_broker(sym, market, broker):
            skipped.append({**a, "reason": f"Symbol '{sym}' ungültig für {broker}/{market}"})
            continue
        out.append(a)
    if skipped:
        log.info("Watchlist: %d aktiv, %d übersprungen (%s)", len(out), len(skipped),
                 "; ".join(f"{s['symbol']}/{s['broker']}: {s['reason']}" for s in skipped[:4]))
    return out, skipped


def _indicator_row(df, last) -> dict:
    price = float(last["close"])
    rsi14 = _safe(last, "rsi14", 50.0)
    bb_up = _safe(last, "bb_upper", price)
    bb_lo = _safe(last, "bb_lower", price)
    return {
        "rsi14": round(rsi14, 2),
        "rsi7": round(_safe(last, "rsi7", 50.0), 2),
        "macd_hist": round(_safe(last, "macd_hist", 0.0), 6),
        "bb_pos": "upper" if price > bb_up else "lower" if price < bb_lo else "mid",
        "ema_cross": "bull" if _safe(last, "ema9", 0) > _safe(last, "ema21", 0) else "bear",
        "volume_ratio": round(_safe(last, "volume_ratio", 1.0), 2),
        "atr14_pct": round(_safe(last, "atr14", 0.0) / price * 100, 3) if price else 0.0,
    }


def gather_features(watchlist: list[dict], exchanges: dict) -> tuple[dict, list[dict], dict]:
    """Holt Bars, berechnet Indikatoren.

    Returns (features, rule_candidates, health).
    features ist nach "broker:symbol" keyed – sonst überschreiben sich identische
    Symbole auf zwei Börsen (BTC/USDT auf BingX *und* Bitunix).
    """
    features: dict[str, dict] = {}
    rule_candidates: list[dict] = []
    health = {"assets_ok": 0, "assets_no_data": 0, "assets_error": 0, "errors": {}}

    for asset in watchlist:
        sym, broker_name = asset["symbol"], asset["broker"]
        ex = exchanges.get(broker_name)
        if not ex:
            continue
        key = f"{broker_name}:{sym}"
        try:
            bars = ex.get_bars(sym, asset["timeframe"], limit=200)
        except Exception as e:  # noqa: BLE001
            health["assets_error"] += 1
            health["errors"][key] = f"{type(e).__name__}: {str(e)[:140]}"
            log.debug("Bars failed for %s: %s", key, e)
            continue
        if not bars or len(bars) < MIN_BARS:
            health["assets_no_data"] += 1
            status = getattr(ex, "data_feed_status", None)
            note = ""
            if callable(status):
                st = status() or {}
                if st.get("rejected_feeds"):
                    note = (f" (Feeds ohne Rechte: {','.join(st['rejected_feeds'])}; "
                            f"ALPACA_DATA_FEED=iex setzen)")
                elif st.get("breaker"):
                    note = f" ({st['breaker']})"
            health["errors"][key] = f"only {len(bars) if bars else 0} bars{note}"
            continue

        df = compute_indicators(bars_to_df(bars))
        last = df.iloc[-1]
        price = float(last["close"])
        features[key] = {
            "symbol": sym, "broker": broker_name, "market": asset["market"],
            "timeframe": asset["timeframe"], "last_close": price,
            "n_bars": len(df), "last_bar": str(last["timestamp"]),
            "indicators": _indicator_row(df, last),
        }
        health["assets_ok"] += 1

        allow_short = asset["market"] in ("crypto_perp", "forex")
        for s in generate_signals(df, sym, broker_name, asset["market"], asset["timeframe"],
                                  allow_short=allow_short):
            rule_candidates.append({
                "symbol": s.symbol, "broker": s.broker, "market": s.market,
                "strategy": s.strategy, "direction": s.direction, "confidence": s.confidence,
                "timeframe": s.timeframe, "entry_price": round(s.entry_price, 6),
                "stop_loss_pct": round(abs(s.entry_price - s.stop_loss) / s.entry_price * 100, 2)
                if s.entry_price else cfg.DEFAULT_STOP_LOSS_PCT,
                "take_profit_pct": round(abs(s.take_profit - s.entry_price) / s.entry_price * 100, 2)
                if s.entry_price else cfg.DEFAULT_TAKE_PROFIT_PCT,
                "rationale": s.note, "rule_indicators": s.indicators,
            })
    return features, rule_candidates, health


def _safe(row, col, default: float = 0.0) -> float:
    try:
        v = row.get(col, default)
        if v is None:
            return default
        v = float(v)
        return default if v != v else v  # NaN → default
    except (TypeError, ValueError):
        return default


def _sanitize_candidates(cands: list[dict], features: dict, exchanges: dict,
                         source: str = "llm") -> list[dict]:
    """Gate: nur Symbole/Broker mit echten Daten, Limits aus dem System-Prompt."""
    known = {(v["broker"], v["symbol"]) for v in features.values()}
    out: list[dict] = []
    dropped = 0
    for c in cands[:20]:
        if not isinstance(c, dict):
            dropped += 1
            continue
        broker = str(c.get("broker") or _guess_broker(str(c.get("symbol", "")), exchanges)).lower()
        sym = normalize_symbol(str(c.get("symbol", "")), broker)
        if (broker, sym) not in known:
            dropped += 1
            log.warning("LLM-Kandidat verworfen (kein Marktdaten-Beweis): %s/%s", sym, broker)
            continue
        direction = str(c.get("direction", "LONG")).upper()
        if direction not in ("LONG", "SHORT"):
            dropped += 1
            continue
        market = features[f"{broker}:{sym}"]["market"]
        if direction == "SHORT" and market not in ("crypto_perp", "forex"):
            dropped += 1
            log.info("SHORT-Kandidat %s verworfen (Markt %s erlaubt keine Leerverkäufe)", sym, market)
            continue
        sl = float(c.get("stop_loss_pct") or cfg.DEFAULT_STOP_LOSS_PCT)
        tp = float(c.get("take_profit_pct") or cfg.DEFAULT_TAKE_PROFIT_PCT)
        sl = max(0.1, min(sl, MAX_SL_PCT))
        tp = max(sl * 2, min(max(tp, sl * 2), MAX_SL_PCT * 4))
        conf = float(c.get("confidence") or 0.5)
        if conf < MIN_CONFIDENCE:
            dropped += 1
            continue
        out.append({
            **c, "symbol": sym, "broker": broker, "market": market,
            "direction": direction, "timeframe": features[f"{broker}:{sym}"]["timeframe"],
            "confidence": round(min(conf, 0.95), 3),
            "stop_loss_pct": round(sl, 3), "take_profit_pct": round(tp, 3),
            "source": c.get("source", source),
            "entry_hint": c.get("entry_hint", "current_price"),
            "data_last_close": features[f"{broker}:{sym}"]["last_close"],
        })
        if len(out) >= MAX_LLM_CANDIDATES:
            break
    if dropped:
        log.info("Kandidaten-Gate (%s): %d verworfen (kein Marktdaten-Beweis / Regeln)",
                 source, dropped)
    return out


def run() -> int:
    t0 = time.time()
    log.info("=== Research Agent start ===")
    hb = Heartbeat(agent="research", last_run="", status="running")
    try:
        killsw, reason = SharedState.killswitch_active()
        if killsw:
            log.warning("Killswitch active, skipping research: %s", reason)
            hb.status, hb.message = "idle", f"killswitch: {reason}"
            return 0

        exchanges = get_exchanges()
        if not exchanges:
            msg = ("keine Broker verbunden – API-Keys in .env setzen "
                   "(für Offline-Tests: ZHF_SYNTH=1 python -m scripts.run_pipeline)")
            if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
                log.warning(msg)
                hb.status, hb.message = "idle", "waiting for exchange API keys (setup)"
                SharedState.set_candidates([])
                SharedState.set_market_data_status({"state": "no_brokers", "detail": msg})
                return 0
            log.error("No exchanges configured – cannot run research.")
            hb.status, hb.message = "error", "no exchanges available"
            return 1

        watchlist, skipped = load_watchlist(exchanges)
        log.info("Watchlist: %d Assets über %d Broker", len(watchlist), len(exchanges))
        features, rule_cands, health = gather_features(watchlist, exchanges)
        log.info("Marktdaten: %d/%d Assets nutzbar, Regel-Vorfilter: %d Kandidaten",
                 health["assets_ok"], len(watchlist), len(rule_cands))
        SharedState.set_market_data_status({
            "state": "ok" if health["assets_ok"] else "degraded",
            "assets": len(watchlist), **health,
            "brokers": {n: (ex.status() or {}) for n, ex in exchanges.items()},
            "skipped_watchlist": skipped[:20],
        })

        if not features:
            # Keine Daten → keine Kandidaten. Kein LLM, keine Signale, kein Trade.
            why = _explain_no_data(health, exchanges)
            log.error("Keine nutzbaren Marktdaten (%s) – Research liefert 0 Kandidaten.", why)
            SharedState.set_candidates([])
            hb.status, hb.message = "error", f"no market data: {why}"
            hb.details = {"health": health, "hint": why}
            return 1

        portfolio = SharedState.portfolio()
        order_state = SharedState.order_state()
        prompt_payload = {
            "watchlist": [{"symbol": v["symbol"], "broker": v["broker"], "market": v["market"],
                           "timeframe": v["timeframe"]} for v in features.values()],
            "bars_pro": features,
            "rule_candidates": rule_cands[:20],
            "portfolio": {"equity": portfolio.get("equity"),
                          "open_positions": [p.get("symbol") for p in portfolio.get("positions", [])]},
            "recent_fills": (order_state.get("fills") or [])[-10:],
            "limits": {"max_positions": cfg.MAX_OPEN_POSITIONS,
                       "max_pos_pct": cfg.MAX_POSITION_SIZE_PCT,
                       "max_stop_loss_pct": MAX_SL_PCT},
        }

        sys_prompt = _load_system_prompt()
        user_prompt = (
            "Analysiere die vorselektierten Handelsideen und Indikatoren unten. "
            "Wähle höchstens 5 der überzeugendsten Kandidaten, bereinige falsche "
            "Signale und schlage ggf. angepasste SL/TP-Werte vor. Nutze ausschliesslich "
            "Symbole aus `bars_pro`/`watchlist` – nichts erfinden. "
            "Gib deine Entscheidung als JSON gemäss deinem Output-Format zurück.\n\n"
            f"MARKTDATEN:\n{json.dumps(prompt_payload, default=str)}"
        )

        # Regel-Kandidaten durch dasselbe Gate wie die LLM-Ausgabe (Symbol gültig,
        # SL <= 2 %, R:R >= 1:2, max. 5) – sonst lehnt Risk sie später ab oder,
        # schlimmer, sie handeln mit unrealistischem Stop-Loss.
        final_candidates = _sanitize_candidates(rule_cands, features, exchanges,
                                                source="rules")
        result: dict[str, Any] = {}
        try:
            result = call_llm_json(user_prompt, system_prompt=sys_prompt,
                                   agent="research", temperature=0.2)
            cands = result.get("candidates", []) if isinstance(result, dict) else []
            if isinstance(cands, list) and cands:
                gated = _sanitize_candidates(cands, features, exchanges, source="llm")
                if gated:
                    final_candidates = gated
                    log.info("LLM: %d Kandidaten akzeptiert (%d verworfen)",
                             len(gated), len(cands) - len(gated))
                else:
                    log.warning("LLM lieferte keine verwertbaren Kandidaten – "
                                "nutze Regel-basierte (%d).", len(rule_cands))
            else:
                log.info("LLM returned empty candidates; falling back to rule-based (%d).",
                         len(rule_cands))
        except LLMError as e:
            log.warning("LLM nicht verfügbar (%s) – rein regelbasierte Kandidaten.", str(e)[:160])

        notes = result.get("notes", "") if isinstance(result, dict) else ""
        if notes:
            log.info("LLM-Marktlage: %s", str(notes)[:200])

        SharedState.set_candidates(final_candidates)
        hb.status = "ok"
        hb.message = (f"{len(final_candidates)} Kandidaten aus {len(features)} Assets "
                      f"({health['assets_no_data']} ohne Daten)")
        hb.details = {"assets_ok": health["assets_ok"], "assets_no_data": health["assets_no_data"],
                      "rule_candidates": len(rule_cands), "notes": str(notes)[:200]}
        log.info("Research done: %d candidates in %.1fs", len(final_candidates), time.time() - t0)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("Research failed: %s", e)
        hb.status, hb.message = "error", str(e)[:200]
        return 1
    finally:
        hb.last_run = now_stamp()
        hb.duration_s = time.time() - t0
        hb.write()


def _explain_no_data(health: dict, exchanges: dict) -> str:
    errs = list(health.get("errors", {}).items())[:3]
    base = ", ".join(f"{k}: {v}" for k, v in errs) or "keine Assets"
    al = exchanges.get("alpaca")
    if al is not None and hasattr(al, "data_feed_status"):
        st = al.data_feed_status() or {}
        if st.get("rejected_feeds"):
            base += " | Alpaca-Feed-Rechte fehlen: ALPACA_DATA_FEED=iex (Free) setzen"
    return base[:300]


def _load_system_prompt() -> str:
    p = Path(cfg.PROMPTS_DIR) / "research_agent.md"
    try:
        return p.read_text()
    except OSError as e:
        log.warning("System-Prompt %s nicht lesbar (%s) – nutze Minimal-Prompt", p, e)
        return ("Du bist der Head of Research eines quantitativen Trading-Teams. "
                "Antworte nur mit JSON: {\"candidates\": [...]} – max 5 Kandidaten, "
                "jeweils symbol/broker/market/timeframe/strategy/direction/confidence/"
                "stop_loss_pct/take_profit_pct/rationale. Keine Daten => leeres Array.")


def _guess_broker(symbol: str, exchanges: dict) -> str:
    s = (symbol or "").upper()
    if "USDT" in s or "USDC" in s:
        for name in ("bingx", "bitunix", "synth"):
            if name in exchanges:
                return name
    if "/" in s:
        return "alpaca"
    return "alpaca"


if __name__ == "__main__":
    sys.exit(run())
