"""Execution Agent – läuft jeden Minute während Marktzeiten.

Liest approved.json, platziert Orders, aktualisiert State, überwacht Positionen.
Rein deterministische Python-Logik – KEIN LLM (Halluzinationsgefahr zu gross).
"""
from __future__ import annotations

import csv
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat
from exchanges.factory import get_exchanges
from exchanges.base import Side, Order, OrderType, OrderStatus

log = get_logger("execution")

FILLS_LOG = cfg.LOG_DIR / "fills.log"
ERROR_TRACK = cfg.DATA_DIR / "orders" / "recent_errors.json"


def _log_fill(order: Order) -> None:
    FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not FILLS_LOG.exists()
    with FILLS_LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "broker", "symbol", "side", "qty", "avg_price",
                        "order_id", "status", "strategy", "notional_usd"])
        w.writerow([
            datetime.now(timezone.utc).isoformat(), order.broker, order.symbol,
            order.side.value, order.filled_qty or order.qty, order.avg_fill_price,
            order.broker_order_id, order.status.value,
            getattr(order, "strategy", ""), round((order.filled_qty or order.qty) * (order.avg_fill_price or 0), 2)
        ])


def _track_error(msg: str) -> None:
    p = ERROR_TRACK
    try:
        existing = []
        if p.exists():
            existing = json.loads(p.read_text())
        existing = existing[-20:]
        existing.append({"ts": datetime.now(timezone.utc).isoformat(), "msg": msg})
        p.write_text(json.dumps(existing, indent=2))
        if len(existing) >= 3:
            # Wenn 3 Fehler in den letzten 5 Minuten → Killswitch
            cutoff = time.time() - 300
            recent = [e for e in existing if time.mktime(time.strptime(e["ts"][:19], "%Y-%m-%dT%H:%M:%S")) > cutoff]
            if len(recent) >= 3:
                SharedState.activate_killswitch(f"3 execution errors in 5 min: {msg[:100]}")
    except Exception:
        pass


def _client_order_id(symbol: str) -> str:
    short = "".join(ch for ch in symbol if ch.isalnum())[:10]
    return f"zhf-{int(time.time())}-{short}-{uuid.uuid4().hex[:6]}"


def run() -> int:
    t0 = time.time()
    log.info("=== Execution Agent start ===")
    hb = Heartbeat(agent="execution", last_run="", status="running")
    try:
        killsw, reason = SharedState.killswitch_active()
        if killsw:
            hb.status = "idle"
            hb.message = f"killswitch: {reason}"
            hb.write(); return 0

        exchanges = get_exchanges()
        if not exchanges:
            if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
                log.info("No exchanges configured – idle during setup.")
                hb.status = "idle"; hb.message = "waiting for exchange API keys (setup)"
                hb.write(); return 0
            _track_error("no exchanges available")
            hb.status = "error"; hb.message = "no exchanges"
            hb.write(); return 1

        # Aktuellen State lesen
        state = SharedState.order_state()
        open_orders = state.get("orders", [])
        fills = state.get("fills", [])

        # Status-Updates für bereits platzierte Orders (Poll)
        remaining_open = []
        for od in open_orders:
            ex = exchanges.get(od["broker"])
            if not ex:
                remaining_open.append(od); continue
            try:
                upd = ex.get_order(od["broker_order_id"])
                if upd.status in (OrderStatus.FILLED,):
                    od["status"] = upd.status.value
                    od["filled_qty"] = upd.filled_qty
                    od["avg_fill_price"] = upd.avg_fill_price
                    _log_fill(upd)
                    fills.append(od)
                    log.info("FILL: %s %s %.4f @ %.4f", upd.symbol, upd.side.value, upd.filled_qty, upd.avg_fill_price)
                elif upd.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                    od["status"] = upd.status.value
                    od["error"] = upd.error
                    fills.append(od)
                    log.warning("Order %s %s: %s", od["broker_order_id"], upd.status.value, upd.error)
                else:
                    # noch offen
                    remaining_open.append(od)
            except Exception as e:
                log.warning("Polling order %s failed: %s", od.get("broker_order_id"), e)
                remaining_open.append(od)

        # Neue Orders platzieren
        approved = SharedState.approved()
        new_orders = []
        for trade in approved:
            broker = trade["broker"]
            ex = exchanges.get(broker)
            if not ex:
                _track_error(f"broker {broker} unavailable for {trade['symbol']}")
                continue
            if not ex.is_market_open(trade["symbol"]):
                log.info("Market closed for %s; skipping.", trade["symbol"])
                continue
            coid = _client_order_id(trade["symbol"])
            order = Order(
                broker=broker,
                symbol=trade["symbol"],
                side=Side.BUY if trade["direction"] == "LONG" else Side.SELL,
                qty=float(trade["qty"]),
                type=OrderType.MARKET if trade.get("order_type", "MARKET") == "MARKET" else OrderType.LIMIT,
                limit_price=trade.get("limit_price"),
                stop_loss=trade.get("stop_loss"),
                take_profit=trade.get("take_profit"),
                leverage=int(trade.get("leverage", 1)),
                client_order_id=coid,
            )
            # Leverage für CCXT-Exchanges setzen (best effort)
            if hasattr(ex, "ex") and trade.get("leverage", 1) > 1 and trade.get("market") == "crypto_perp":
                try:
                    ex.ex.set_leverage(trade["leverage"], trade["symbol"])
                except Exception as e:
                    log.warning("Could not set leverage on %s: %s", broker, e)
            try:
                placed = ex.place_order(order)
                new_orders.append({
                    "broker": placed.broker, "symbol": placed.symbol,
                    "side": placed.side.value, "qty": placed.qty,
                    "type": placed.type.value, "status": placed.status.value,
                    "broker_order_id": placed.broker_order_id,
                    "client_order_id": coid, "stop_loss": placed.stop_loss,
                    "take_profit": placed.take_profit, "leverage": placed.leverage,
                    "strategy": trade.get("strategy"),
                    "direction": trade["direction"],
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                    "error": placed.error,
                    "avg_fill_price": placed.avg_fill_price,
                    "filled_qty": placed.filled_qty,
                })
                if placed.status == OrderStatus.FILLED:
                    fills.append(new_orders[-1])
                    _log_fill(placed)
                    log.info("IMMEDIATE FILL: %s %s %.4f @ %.4f",
                             placed.symbol, placed.side.value, placed.filled_qty or placed.qty, placed.avg_fill_price)
                elif placed.status == OrderStatus.REJECTED:
                    _track_error(f"Order rejected: {trade['symbol']} {placed.error}")
                else:
                    log.info("Submitted order %s for %s %s", placed.broker_order_id, trade["direction"], trade["symbol"])
            except Exception as e:
                _track_error(f"place_order exception for {trade['symbol']}: {e}")

        # Approved-Liste leeren wenn alle verarbeitet (damit Risk nicht nochmal schickt)
        SharedState.set_approved([])

        # State zusammenführen: offene (rest) + neue, Fills anhängen
        final_open = remaining_open + [o for o in new_orders if o["status"] in ("NEW", "PARTIAL", "PARTIALLY_FILLED")]
        SharedState.update_order_state(final_open, fills[-200:])  # letzten 200 Fills behalten
        log.info("Execution cycle done: %d open orders, %d fills total", len(final_open), len(fills))

        # Portfolio-Snapshot aktualisieren
        total_eq = 0.0; total_cash = 0.0; positions = []
        for name, ex in exchanges.items():
            try:
                acc = ex.get_account()
                total_eq += acc.get("equity", 0)
                total_cash += acc.get("cash", 0)
                for p in ex.get_positions():
                    positions.append({
                        "symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry,
                        "broker": p.broker, "unrealized_pnl": p.unrealized_pnl,
                        "market": p.market, "leverage": p.leverage,
                    })
            except Exception as e:
                log.warning("Portfolio fetch failed for %s: %s", name, e)
        SharedState.set_portfolio(total_eq, total_cash, positions, "execution")

        hb.status = "ok"
        hb.message = f"open={len(final_open)} submitted={len(new_orders)} equity={total_eq:.2f}"
        return 0
    except Exception as e:
        log.exception("Execution failed: %s", e)
        _track_error(str(e))
        hb.status = "error"; hb.message = str(e)[:200]; return 1
    finally:
        hb.last_run = time.strftime("%Y-%m-%dT%H:%M:%S")
        hb.duration_s = time.time() - t0
        hb.write()


if __name__ == "__main__":
    sys.exit(run())
