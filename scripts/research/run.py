"""Research Agent Worker.

Wird vom Paperclip-Heartbeat oder manuell aufgerufen:
  python -m scripts.research.run

Lädt Watchlist, holt Bars für alle Assets, berechnet Indikatoren, gibt die
kompakte Feature-Matrix an das LLM (primär OpenCode big-pickle, Fallback
lokal) weiter und speichert Kandidaten.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Pfad-Konfiguration für Script-Aufruf
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.llm import call_llm_json, LLMError
from scripts.common.state import SharedState, Heartbeat
from exchanges.factory import get_exchanges
from strategies.signals import bars_to_df, compute_indicators, generate_signals

log = get_logger("research")

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

# BingX/Bitunix Perps (werden nur hinzugefügt wenn Keys gesetzt sind)
PERP_WATCHLIST = [
    {"symbol": "BTC/USDT",  "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "ETH/USDT",  "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "SOL/USDT",  "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "DOGE/USDT", "broker": "bingx",  "market": "crypto_perp", "timeframe": "1h"},
    {"symbol": "BTC/USDT",  "broker": "bitunix", "market": "crypto_perp", "timeframe": "1h"},
]


def load_watchlist() -> list[dict]:
    p = Path(cfg.STRATEGIES_DIR) / "watchlist.json"
    if p.exists():
        try:
            with p.open() as f:
                wl = json.load(f)
                if isinstance(wl, list) and wl:
                    return wl
        except Exception as e:
            log.warning("Could not load watchlist, using default: %s", e)
    wl = list(DEFAULT_WATCHLIST)
    if cfg.BINGX_API_KEY and "your_" not in cfg.BINGX_API_KEY:
        wl.extend([a for a in PERP_WATCHLIST if a["broker"] == "bingx"])
    if cfg.BITUNIX_API_KEY and "your_" not in cfg.BITUNIX_API_KEY:
        wl.extend([a for a in PERP_WATCHLIST if a["broker"] == "bitunix"])
    return wl


def gather_features(watchlist: list[dict], exchanges: dict) -> tuple[dict, list[dict]]:
    """Holt Bars, berechnet Indikatoren. Returns (pro_features, rule_candidates)."""
    pro: dict = {}
    rule_candidates: list[dict] = []
    for asset in watchlist:
        sym = asset["symbol"]
        broker_name = asset["broker"]
        ex = exchanges.get(broker_name)
        if not ex:
            continue
        try:
            bars = ex.get_bars(sym, asset["timeframe"], limit=200)
        except Exception as e:
            log.warning("Bars failed for %s/%s: %s", broker_name, sym, e)
            continue
        if not bars or len(bars) < 30:
            log.info("Not enough bars for %s: %d", sym, len(bars))
            continue
        df = bars_to_df(bars)
        df = compute_indicators(df)
        last = df.iloc[-1]
        pro[sym] = {
            "broker": broker_name, "market": asset["market"], "timeframe": asset["timeframe"],
            "last_close": float(last["close"]),
            "indicators": {
                "rsi14": _safe(last, "rsi14"), "rsi7": _safe(last, "rsi7"),
                "macd_hist": _safe(last, "macd_hist"),
                "bb_pos": "upper" if float(last["close"]) > _safe(last, "bb_upper", 9e9) else
                          "lower" if float(last["close"]) < _safe(last, "bb_lower", 0) else "mid",
                "ema_cross": "bull" if _safe(last, "ema9", 0) > _safe(last, "ema21", 0) else "bear",
                "volume_ratio": _safe(last, "volume_ratio", 1),
                "atr14_pct": round(_safe(last, "atr14", 0) / float(last["close"]) * 100, 3),
            },
        }
        # Regel-basierte Signale als Vorfilter
        allow_short = asset["market"] in ("crypto_perp", "forex")
        sigs = generate_signals(df, sym, broker_name, asset["market"], asset["timeframe"],
                                allow_short=allow_short)
        for s in sigs:
            rule_candidates.append({
                "symbol": s.symbol, "broker": s.broker, "market": s.market,
                "strategy": s.strategy, "direction": s.direction, "confidence": s.confidence,
                "timeframe": s.timeframe,
                "stop_loss_pct": round(abs(s.entry_price - s.stop_loss) / s.entry_price * 100, 2),
                "take_profit_pct": round(abs(s.take_profit - s.entry_price) / s.entry_price * 100, 2),
                "rationale": s.note,
                "rule_indicators": s.indicators,
            })
    return pro, rule_candidates


def _safe(row, col, default=0.0) -> float:
    try:
        v = row.get(col, default)
        return float(v) if v is not None else default
    except Exception:
        return default


def run() -> int:
    t0 = time.time()
    log.info("=== Research Agent start ===")
    hb = Heartbeat(agent="research", last_run="", status="running")
    try:
        killsw, reason = SharedState.killswitch_active()
        if killsw:
            log.warning("Killswitch active, skipping research: %s", reason)
            hb.status = "idle"
            hb.message = f"killswitch: {reason}"
            hb.duration_s = time.time() - t0
            hb.write()
            return 0

        exchanges = get_exchanges()
        if not exchanges:
            msg = "no exchanges configured – set API keys in .env to run research (in DRY_RUN/paper this is OK during setup)"
            if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
                log.warning(msg)
                hb.status = "idle"
                hb.message = "waiting for exchange API keys (setup)"
                hb.duration_s = time.time() - t0
                hb.write()
                return 0
            log.error("No exchanges configured – cannot run research.")
            hb.status = "error"
            hb.message = "no exchanges available"
            hb.duration_s = time.time() - t0
            hb.write()
            return 1

        watchlist = load_watchlist()
        log.info("Watchlist: %d assets across %d exchanges", len(watchlist), len(exchanges))
        pro_features, rule_cands = gather_features(watchlist, exchanges)
        log.info("Rule-based pre-filter: %d candidates", len(rule_cands))

        portfolio = SharedState.portfolio()
        open_positions_symbols = [p.get("symbol") for p in portfolio.get("positions", [])]

        # LLM-Filter auf den vorselektierten Kandidaten (spart Tokens!)
        prompt_payload = {
            "rule_candidates": rule_cands[:20],  # begrenzen auf Kontext
            "features": {k: v for k, v in pro_features.items() if v is not None},
            "portfolio": {"equity": portfolio.get("equity"), "open_positions": open_positions_symbols},
            "limits": {
                "max_positions": cfg.MAX_OPEN_POSITIONS,
                "max_pos_pct": cfg.MAX_POSITION_SIZE_PCT,
            },
        }

        # Wir laden den System-Prompt
        sys_prompt = (Path(cfg.STRATEGIES_DIR).parent / "prompts" / "research_agent.md").read_text()
        user_prompt = (
            "Analysiere die vorselektierten Handelsideen und Indikatoren unten. "
            "Wähle höchstens 5 der überzeugendsten Kandidaten, bereinige falsche "
            "Signale und schlage ggf. angepasste SL/TP-Werte vor. "
            "Gib deine Entscheidung als JSON gemäss deinem Output-Format zurück.\n\n"
            f"MARKTDATEN:\n{json.dumps(prompt_payload, default=str)}"
        )

        final_candidates = rule_cands  # Fallback: Regeln allein
        try:
            result = call_llm_json(user_prompt, system_prompt=sys_prompt,
                                   agent="research", temperature=0.2)
            cands = result.get("candidates", [])
            if isinstance(cands, list) and cands:
                final_candidates = []
                for c in cands:
                    if all(k in c for k in ("symbol", "direction", "strategy")):
                        c.setdefault("broker", _guess_broker(c["symbol"], exchanges))
                        c.setdefault("market", "crypto_perp" if "USDT" in c.get("symbol", "") else "stocks")
                        c.setdefault("timeframe", "1h")
                        c.setdefault("stop_loss_pct", cfg.DEFAULT_STOP_LOSS_PCT)
                        c.setdefault("take_profit_pct", cfg.DEFAULT_TAKE_PROFIT_PCT)
                        c.setdefault("confidence", 0.5)
                        final_candidates.append(c)
                log.info("LLM selected %d candidates", len(final_candidates))
            else:
                log.info("LLM returned empty candidates; falling back to rule-based (%d).", len(rule_cands))
        except LLMError as e:
            log.error("LLM call failed, using pure rule-based candidates: %s", e)

        # Normalisiere Symbol/Broker für die Weiterverarbeitung
        for c in final_candidates:
            if not c.get("broker"):
                c["broker"] = _guess_broker(c["symbol"], exchanges)

        SharedState.set_candidates(final_candidates)
        hb.status = "ok"
        hb.message = f"{len(final_candidates)} candidates from {len(pro_features)} assets"
        log.info("Research done: %d candidates in %.1fs", len(final_candidates), time.time() - t0)
        return 0
    except Exception as e:
        log.exception("Research failed: %s", e)
        hb.status = "error"
        hb.message = str(e)[:200]
        return 1
    finally:
        hb.last_run = time.strftime("%Y-%m-%dT%H:%M:%S")
        hb.duration_s = time.time() - t0
        hb.write()


def _guess_broker(symbol: str, exchanges: dict) -> str:
    if "USDT" in symbol:
        if "bingx" in exchanges:
            return "bingx"
        if "bitunix" in exchanges:
            return "bitunix"
    if "/" in symbol:
        return "alpaca"
    return "alpaca"


if __name__ == "__main__":
    sys.exit(run())
