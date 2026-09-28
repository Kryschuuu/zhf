"""Risk Management Agent – läuft als Paperclip Prozess-Heartbeat.

Liest validierte Signale, Portfolio-State und Fee-Report; prüft gegen
Hard-Limits; berechnet Positionsgrössen; schreibt approved.json oder
aktiviert Killswitch.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat
from exchanges.factory import get_exchanges
from exchanges.base import Side, Order, OrderType

log = get_logger("risk")


def _correlation(a_symbol: str, b_symbols: list[str], exchanges: dict, broker: str, tf: str = "1h") -> float:
    """Schätzt Korrelation zwischen a_symbol und jedem Symbol in b_symbols anhand der Returns."""
    if not b_symbols:
        return 0.0
    ex = exchanges.get(broker)
    if not ex:
        return 0.0
    try:
        bars_a = ex.get_bars(a_symbol, tf, limit=100)
        if len(bars_a) < 30:
            return 0.0
        s_a = pd.Series([b.close for b in bars_a]).pct_change().dropna()
        max_corr = 0.0
        for bs in b_symbols:
            # Finde richtigen Broker für bs
            b_broker = broker
            bars_b = ex.get_bars(bs, tf, limit=100)
            if len(bars_b) < 30:
                continue
            s_b = pd.Series([b.close for b in bars_b]).pct_change().dropna()
            n = min(len(s_a), len(s_b))
            if n < 20:
                continue
            c = np.corrcoef(s_a.iloc[-n:], s_b.iloc[-n:])[0, 1]
            if abs(c) > max_corr:
                max_corr = abs(c)
        return float(max_corr)
    except Exception as e:
        log.warning("correlation calc failed for %s: %s", a_symbol, e)
        return 0.0


def run() -> int:
    t0 = time.time()
    log.info("=== Risk Agent start ===")
    hb = Heartbeat(agent="risk", last_run="", status="running")
    try:
        # Circuit Breaker: lies Fills/Portfolio und prüfe Drawdown
        exchanges = get_exchanges()
        if not exchanges:
            if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
                log.info("No exchanges configured – idle during setup.")
                hb.status = "idle"; hb.message = "waiting for exchange API keys (setup)"
                hb.write(); return 0
            hb.status = "error"; hb.message = "no exchanges"; hb.write(); return 1

        # Aggregate portfolio across all brokers
        total_equity = 0.0; total_cash = 0.0; all_positions: list[dict] = []
        for name, ex in exchanges.items():
            try:
                acc = ex.get_account()
                total_equity += acc.get("equity", 0)
                total_cash += acc.get("cash", 0)
                for p in ex.get_positions():
                    all_positions.append({
                        "symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry,
                        "broker": p.broker, "unrealized_pnl": p.unrealized_pnl,
                        "market": p.market, "leverage": p.leverage,
                    })
            except Exception as e:
                log.warning("Could not fetch %s account: %s", name, e)
        SharedState.set_portfolio(total_equity, total_cash, all_positions, "multi-broker")

        # Daily/Weekly PnL prüfen (sehr einfach: aus Fills-Log)
        day_pnl = _daily_pnl_pct(total_equity)
        if abs(day_pnl) > cfg.MAX_DAILY_DRAWDOWN_PCT and day_pnl < 0:
            SharedState.activate_killswitch(
                f"Daily drawdown {day_pnl:.2f}% > {cfg.MAX_DAILY_DRAWDOWN_PCT}%")
            hb.status = "error"; hb.message = f"daily dd {day_pnl:.2f}%"
            hb.write(); return 1

        # Bereits approved? Dann nichts tun (Execution arbeitet die Liste ab)
        approved_now = SharedState.approved()
        if approved_now:
            log.info("Approved list already has %d trades; waiting for execution.", len(approved_now))
            hb.status = "ok"; hb.message = f"waiting for execution of {len(approved_now)} orders"
            hb.write(); return 0

        validated = SharedState.validated()
        if not validated:
            hb.status = "ok"; hb.message = "no validated trades"
            hb.write(); return 0

        approved: list[dict] = []
        rejected: list[dict] = []
        open_symbols = [p["symbol"] for p in all_positions]

        # Limit: Max-Positionen
        if len(all_positions) >= cfg.MAX_OPEN_POSITIONS:
            hb.status = "ok"; hb.message = f"max positions reached ({len(all_positions)})"
            SharedState.set_approved([])
            hb.write(); return 0

        for c in validated:
            sym = c.get("symbol"); broker = c.get("broker", "alpaca")
            direction = c.get("direction", "LONG")
            size_pct = c.get("position_size_pct_hint", 1.0)
            sl_pct = c.get("stop_loss_pct", cfg.DEFAULT_STOP_LOSS_PCT)
            tp_pct = c.get("take_profit_pct", cfg.DEFAULT_TAKE_PROFIT_PCT)
            leverage = 1
            if c.get("market") in ("crypto_perp", "forex"):
                leverage = min(cfg.MAX_LEVERAGE, 2)  # konservativ
            if c.get("market") == "stocks":
                leverage = 1

            # Hard limits
            if size_pct > cfg.MAX_POSITION_SIZE_PCT:
                rejected.append({**c, "reason": f"size {size_pct}% > max {cfg.MAX_POSITION_SIZE_PCT}%"})
                continue
            if tp_pct / max(sl_pct, 0.01) < 2.0:
                rejected.append({**c, "reason": "R:R < 1:2"})
                continue
            if direction == "SHORT" and c.get("market") == "stocks":
                rejected.append({**c, "reason": "no short selling on stocks in first phase"})
                continue

            # Korrelationsprüfung
            corr = _correlation(sym, open_symbols, exchanges, broker)
            if corr > cfg.CORRELATION_LIMIT:
                rejected.append({**c, "reason": f"correlation {corr:.2f} > {cfg.CORRELATION_LIMIT} with existing positions"})
                continue

            # Exchanges/Broker verfügbar?
            ex = exchanges.get(broker)
            if not ex:
                rejected.append({**c, "reason": f"broker {broker} unavailable"})
                continue
            if not ex.is_market_open(sym):
                rejected.append({**c, "reason": "market closed"})
                continue

            # Preis holen
            try:
                bars = ex.get_bars(sym, c.get("timeframe", "1h"), limit=2)
                if not bars:
                    rejected.append({**c, "reason": "no recent price"}); continue
                price = bars[-1].close
            except Exception as e:
                rejected.append({**c, "reason": f"price fetch failed: {e}"}); continue

            # Positionsgrösse berechnen (in Notional USD)
            risk_usd = total_equity * (size_pct / 100)
            qty_notional = risk_usd / (sl_pct / 100) if sl_pct > 0 else total_equity * 0.01
            qty_notional = min(qty_notional, total_cash * 0.95 / max(leverage, 1))
            qty_notional = min(qty_notional, total_equity * cfg.MAX_POSITION_SIZE_PCT / 100 / (sl_pct / 100))
            if c.get("market") == "stocks":
                # Bei Aktien in USD notional (fraktional möglich)
                qty = round(qty_notional / price, 4)
            else:
                qty = round(qty_notional / price, 6)
            if qty * price < 5:  # Mindest-Ordergrösse für die meisten Broker
                rejected.append({**c, "reason": "position too small (min $5)"})
                continue

            sl_price = price * (1 - sl_pct / 100) if direction == "LONG" else price * (1 + sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100) if direction == "LONG" else price * (1 - tp_pct / 100)

            approved.append({
                "symbol": sym,
                "broker": broker,
                "direction": direction,
                "strategy": c.get("strategy"),
                "order_type": "MARKET",
                "qty": qty,
                "leverage": leverage,
                "limit_price": None,
                "stop_loss": round(sl_price, 6),
                "take_profit": round(tp_price, 6),
                "entry_reference_price": round(price, 6),
                "notional_usd": round(qty * price, 2),
                "risk_usd": round(qty * price * sl_pct / 100, 2),
                "max_loss_pct": sl_pct,
                "rationale": f"BT: n={c['backtest']['n_trades']}, WR={c['backtest']['win_rate']}, PF={c['backtest']['profit_factor']}, corr={corr:.2f}",
            })

            # nur einen Trade pro Risk-Durchlauf, um Überkonzentration zu vermeiden
            break

        SharedState.set_approved(approved)
        log.info("Risk approved %d / %d candidates", len(approved), len(validated))
        hb.status = "ok"
        hb.message = f"approved={len(approved)} rejected={len(rejected)} equity={total_equity:.2f}"
        return 0
    except Exception as e:
        log.exception("Risk failed: %s", e)
        hb.status = "error"; hb.message = str(e)[:200]; return 1
    finally:
        hb.last_run = time.strftime("%Y-%m-%dT%H:%M:%S")
        hb.duration_s = time.time() - t0
        hb.write()


def _daily_pnl_pct(current_equity: float) -> float:
    """Lese Tagesanfangs-Equity aus der .equity_log-Datei; grobe Schätzung."""
    p = cfg.DATA_DIR / "equity_log.jsonl"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start_eq = None
    if p.exists():
        try:
            lines = p.read_text().strip().split("\n")
            for line in lines:
                if not line: continue
                d = json.loads(line)
                ts = d.get("ts", "")
                if ts.startswith(today) and "equity" in d:
                    if day_start_eq is None:
                        day_start_eq = d["equity"]
        except Exception:
            pass
    # Logge aktuelle Equity
    with p.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "equity": current_equity}) + "\n")
    if day_start_eq and day_start_eq > 0:
        return (current_equity - day_start_eq) / day_start_eq * 100
    return 0.0


if __name__ == "__main__":
    sys.exit(run())
