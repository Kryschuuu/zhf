"""Execution Agent – läuft jede Minute während Marktzeiten.

Liest approved.json, platziert Orders, aktualisiert State, überwacht Positionen.
Rein deterministische Python-Logik – KEIN LLM (Halluzinationsgefahr zu gross).

Korrekturen:
- DRY_RUN-Fills hatten `avg_fill_price = 0` → `notional_usd = 0` im Fills-Log →
  Cost-Optimizer und Slippage-Messung blind. Jetzt Fill zum Referenzpreis.
- Slippage wird gemessen (Referenzpreis aus Risk vs. Ausführungspreis).
- `recent_errors` benutzte `time.mktime(strptime(...))` (lokale Zeit) für das
  5-Minuten-Fenster → auf einer CET-Maschine tickte die Killswitch-Logik zwei
  Stunden falsch. Jetzt echte UTC-Timestamps.
- Nicht verarbeitbare Trades (Broker offline / read-only / Markt zu) wurden
  kommentarlos verworfen, weil approved.json am Zyklusende geleert wurde.
  Jetzt bis zu 3 Retries, danach dokumentierter Drop + Grund im Log.
- Gebühren/Slippage landen im Fills-Log, damit der Cost-Optimizer mit echten
  Zahlen rechnet statt mit 0.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat, now_stamp
from scripts.common.fees import expected_costs
from scripts.common.fills import append_fill
from exchanges.factory import get_exchanges
from exchanges.base import Side, Order, OrderType, OrderStatus

log = get_logger("execution")

FILLS_LOG = cfg.LOG_DIR / "fills.log"      # Kompatibilität für Tests/Watchdog
ERROR_TRACK = cfg.DATA_DIR / "orders" / "recent_errors.json"
FILL_COLUMNS = None                        # deprecated: Schema liegt in scripts.common.fills
MAX_ORDER_ATTEMPTS = 3
ERROR_WINDOW_S = 300


# --------------------------------------------------------------------- logging
def _ref_price(trade: dict, order: Order) -> float:
    for key in ("entry_reference_price", "data_last_close", "limit_price"):
        v = trade.get(key)
        try:
            if v and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return float(order.limit_price or 0.0)


def _log_fill(order: Order, trade: dict | None = None) -> dict:
    """Ein Fill als CSV-Zeile; gibt die berechneten Kosten zurück (fürs State)."""
    trade = trade or {}
    qty = float(order.filled_qty or order.qty or 0)
    price = float(order.avg_fill_price or 0)
    ref = _ref_price(trade, order)
    notional = round(qty * price, 4)
    slip_bps = round((price - ref) / ref * 10_000, 2) if ref > 0 and price > 0 else ""
    costs = expected_costs(notional, order.broker, order.market or trade.get("market", ""))
    append_fill({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "broker": order.broker, "symbol": order.symbol, "side": order.side.value,
        "qty": qty, "avg_price": price, "order_id": order.broker_order_id or "",
        "status": order.status.value, "strategy": trade.get("strategy", ""),
        "notional_usd": notional, "ref_price": round(ref, 6) if ref else "",
        "slippage_bps": slip_bps, "est_fee_usd": costs["commission_usd"],
        "mode": "dry_run" if cfg.DRY_RUN else ("synth" if order.broker == "synth" else "live"),
    })
    return {"notional_usd": notional, "slippage_bps": slip_bps,
            "est_fee_usd": costs["commission_usd"], "ref_price": round(ref, 6) if ref else None}


def _track_error(msg: str) -> None:
    """Fehlerprotokoll; >=3 in ERROR_WINDOW_S → Killswitch (Broker-/Order-Sturm)."""
    now = datetime.now(timezone.utc)
    entry = {"ts": now.isoformat(), "msg": str(msg)[:300]}
    existing: list[dict] = []
    try:
        if ERROR_TRACK.exists():
            loaded = json.loads(ERROR_TRACK.read_text() or "[]")
            if isinstance(loaded, list):
                existing = loaded[-20:]
    except (json.JSONDecodeError, OSError):
        existing = []
    existing.append(entry)
    existing = existing[-40:]
    try:
        ERROR_TRACK.parent.mkdir(parents=True, exist_ok=True)
        ERROR_TRACK.write_text(json.dumps(existing, indent=2))
    except OSError as e:
        log.warning("recent_errors.json nicht schreibbar: %s", e)
    recent = 0
    for e in existing:
        try:
            ts = datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() <= ERROR_WINDOW_S:
                recent += 1
        except ValueError:
            continue
    if recent >= 3:
        SharedState.activate_killswitch(
            f"{recent} execution errors in {ERROR_WINDOW_S // 60} min: {msg[:100]}")


def _client_order_id(symbol: str) -> str:
    short = "".join(ch for ch in symbol if ch.isalnum())[:10] or "sym"
    return f"zhf-{int(time.time())}-{short}-{uuid.uuid4().hex[:6]}"[:64]


def _trade_to_order(trade: dict, broker: str, coid: str) -> Order:
    def _f(key: str) -> float | None:
        v = trade.get(key)
        try:
            return float(v) if v not in (None, "", 0) else None
        except (TypeError, ValueError):
            return None
    return Order(
        broker=broker,
        symbol=trade["symbol"],
        side=Side.BUY if str(trade.get("direction", "LONG")).upper() == "LONG" else Side.SELL,
        qty=float(trade.get("qty") or 0),
        type=OrderType.MARKET if str(trade.get("order_type", "MARKET")).upper() == "MARKET" else OrderType.LIMIT,
        limit_price=_f("limit_price"),
        stop_loss=_f("stop_loss"),
        take_profit=_f("take_profit"),
        leverage=int(trade.get("leverage") or 1),
        market=str(trade.get("market") or ""),
        entry_reference_price=_f("entry_reference_price"),
        client_order_id=coid,
    )


def _order_to_dict(o: Order, trade: dict, coid: str, cost: dict | None = None) -> dict:
    return {
        "broker": o.broker, "symbol": o.symbol, "side": o.side.value, "qty": o.qty,
        "type": o.type.value, "status": o.status.value,
        "broker_order_id": o.broker_order_id, "client_order_id": coid,
        "stop_loss": o.stop_loss, "take_profit": o.take_profit, "leverage": o.leverage,
        "market": o.market, "strategy": trade.get("strategy"),
        "direction": trade.get("direction"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "error": o.error, "avg_fill_price": o.avg_fill_price, "filled_qty": o.filled_qty,
        "entry_reference_price": o.entry_reference_price,
        "notional_usd": trade.get("notional_usd"), "cost": cost or {},
    }


def _reconcile_positions(exchanges: dict, positions: list[dict]) -> list[str]:
    """Notiert offene Positionen gegen ihre SL/TP – Exit-Signale für den nächsten Zyklus.

    Der Broker übernimmt bei Alpaca (OTO) und Bitunix (slPrice/tpPrice) die
    Absicherung; für den Fall, dass ein Broker keine Attached-Orders unterstützt,
    erkennt der Bot hier mindestens den SL-Bruch und meldet ihn laut.
    """
    alerts: list[str] = []
    for pos in positions:
        sym = pos.get("symbol")
        ex = exchanges.get(pos.get("broker"))
        if not ex or not sym:
            continue
        try:
            bars = ex.get_bars(sym, "1h", limit=3)
        except Exception:  # noqa: BLE001
            continue
        if not bars:
            continue
        price = float(bars[-1].close)
        entry = float(pos.get("avg_entry") or 0)
        qty = float(pos.get("qty") or 0)
        if entry <= 0 or qty == 0:
            continue
        pnl_pct = (price / entry - 1) * 100 * (1 if qty > 0 else -1)
        if pnl_pct <= -cfg.DEFAULT_STOP_LOSS_PCT * 3:
            alerts.append(f"{sym}@{pos.get('broker')}: {pnl_pct:.2f}% unter SL-Nähe – Exit prüfen")
    return alerts


def run() -> int:
    t0 = time.time()
    log.info("=== Execution Agent start ===")
    hb = Heartbeat(agent="execution", last_run="", status="running")
    try:
        killsw, reason = SharedState.killswitch_active()
        if killsw:
            log.warning("Killswitch aktiv – keine Orders. Grund: %s", reason)
            hb.status, hb.message = "idle", f"killswitch: {reason}"
            return 0

        exchanges = get_exchanges()
        if not exchanges:
            if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
                log.info("Keine Broker verbunden – Execution im Leerlauf (Setup-Phase).")
                hb.status, hb.message = "idle", "waiting for exchange API keys (setup)"
                return 0
            _track_error("no exchanges available")
            hb.status, hb.message = "error", "no exchanges"
            return 1

        state = SharedState.order_state()
        open_orders = list(state.get("orders", []))
        fills = list(state.get("fills", []))

        # 1) Status-Updates für bereits platzierte Orders (Poll)
        remaining_open: list[dict] = []
        newly_filled: list[dict] = []
        for od in open_orders:
            ex = exchanges.get(od.get("broker", ""))
            oid = od.get("broker_order_id")
            if not ex or not oid:
                remaining_open.append(od)
                continue
            try:
                upd = ex.get_order(oid)
            except Exception as e:  # noqa: BLE001
                log.warning("Polling order %s failed: %s", oid, str(e)[:150])
                remaining_open.append(od)
                continue
            st = upd.status
            if st == OrderStatus.FILLED:
                od.update({"status": st.value, "filled_qty": upd.filled_qty,
                           "avg_fill_price": upd.avg_fill_price,
                           "cost": _log_fill(upd, od)})
                newly_filled.append(od)
                log.info("FILL: %s %s %.6f @ %.4f", upd.symbol, upd.side.value,
                         upd.filled_qty, upd.avg_fill_price)
            elif st in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                od.update({"status": st.value, "error": upd.error})
                newly_filled.append(od)
                log.warning("Order %s %s: %s", oid, st.value, str(upd.error)[:150])
            else:
                remaining_open.append(od)

        # 2) Neue Orders aus approved.json (+ Retries aus dem Vorlauf)
        approved = SharedState.approved()
        queue: list[dict] = []
        for trade in approved:
            t = dict(trade)
            t.setdefault("attempts", 0)
            queue.append(t)

        new_orders: list[dict] = []
        requeue: list[dict] = []
        skipped: dict[str, int] = {}
        for trade in queue:
            broker = str(trade.get("broker", "")).lower()
            sym = str(trade.get("symbol", ""))
            trade["attempts"] = int(trade.get("attempts", 0)) + 1
            ex = exchanges.get(broker)
            if not ex:
                skipped[f"broker {broker} fehlt"] = skipped.get(f"broker {broker} fehlt", 0) + 1
                _track_error(f"broker {broker} unavailable for {sym}")
                if trade["attempts"] < MAX_ORDER_ATTEMPTS:
                    requeue.append(trade)
                continue
            if getattr(ex, "read_only", False):
                msg = f"{broker} read-only – Order blockiert"
                log.error("%s (%s)", msg, sym)
                skipped[msg] = skipped.get(msg, 0) + 1
                if trade["attempts"] < MAX_ORDER_ATTEMPTS:
                    requeue.append(trade)
                continue
            if not ex.is_market_open(sym):
                log.info("Markt geschlossen für %s – vertagt (%d/%d).", sym,
                         trade["attempts"], MAX_ORDER_ATTEMPTS)
                skipped["market closed"] = skipped.get("market closed", 0) + 1
                if trade["attempts"] < MAX_ORDER_ATTEMPTS:
                    requeue.append(trade)
                continue

            coid = trade.get("client_order_id") or _client_order_id(sym)
            order = _trade_to_order(trade, broker, coid)
            if order.qty <= 0:
                log.error("Order für %s verworfen: qty=%s unsinnig", sym, trade.get("qty"))
                skipped["qty<=0"] = skipped.get("qty<=0", 0) + 1
                _track_error(f"invalid qty for {sym}: {trade.get('qty')}")
                continue
            # Hebel für Perps (best effort – Scheitern ist kein Grund die Order zu killen)
            if order.leverage > 1 and "perp" in (order.market or ""):
                try:
                    if not ex.set_leverage(sym, order.leverage):
                        log.info("%s: Hebel %sx nicht gesetzt (API ignoriert/nicht unterstützt)",
                                 broker, order.leverage)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not set leverage on %s: %s", broker, str(e)[:120])
            try:
                placed = ex.place_order(order)
            except Exception as e:  # noqa: BLE001
                _track_error(f"place_order exception for {sym}: {e}")
                skipped["exception"] = skipped.get("exception", 0) + 1
                if trade["attempts"] < MAX_ORDER_ATTEMPTS:
                    requeue.append(trade)
                continue
            cost = {}
            if placed.status == OrderStatus.FILLED:
                cost = _log_fill(placed, trade)
            rec = _order_to_dict(placed, trade, coid, cost)
            new_orders.append(rec)
            if placed.status == OrderStatus.FILLED:
                newly_filled.append(rec)
                log.info("IMMEDIATE FILL: %s %s %.6f @ %.4f (ref %.4f, slip %s bps)",
                         placed.symbol, placed.side.value, placed.filled_qty or placed.qty,
                         placed.avg_fill_price, placed.entry_reference_price or 0,
                         cost.get("slippage_bps", "-"))
            elif placed.status == OrderStatus.REJECTED:
                _track_error(f"Order rejected: {sym} {placed.error}")
                skipped[f"rejected: {str(placed.error)[:40]}"] = \
                    skipped.get(f"rejected: {str(placed.error)[:40]}", 0) + 1
            else:
                log.info("Order %s für %s %s submitted", placed.broker_order_id,
                         trade.get("direction"), sym)

        # 3) Approved-Liste: nur verarbeitete raus, Rest zurückgeben
        if requeue:
            SharedState.set_approved(requeue)
            log.warning("%d Trades für nächsten Zyklus zurückgelegt (%s)", len(requeue),
                        ", ".join(f"{k}×{v}" for k, v in skipped.items()))
        else:
            SharedState.set_approved([])

        final_open = remaining_open + [o for o in new_orders
                                       if o["status"] in ("NEW", "PARTIAL", "PARTIALLY_FILLED")]
        all_fills = (fills + newly_filled)[-200:]
        SharedState.update_order_state(final_open, all_fills)
        log.info("Execution cycle done: %d offen, %d Fills, %d neu, %d zurückgelegt%s",
                 len(final_open), len(all_fills), len(new_orders), len(requeue),
                 f" | skipped: {dict(skipped)}" if skipped else "")

        # 4) Portfolio-Snapshot + Exit-Warnungen
        total_eq = 0.0
        total_cash = 0.0
        positions: list[dict] = []
        errors: dict[str, str] = {}
        for name, ex in exchanges.items():
            try:
                acc = ex.get_account()
                if acc.get("error"):
                    errors[name] = str(acc["error"])[:150]
                total_eq += float(acc.get("equity") or 0)
                total_cash += float(acc.get("cash") or 0)
                for p in ex.get_positions():
                    positions.append({
                        "symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry,
                        "broker": p.broker, "unrealized_pnl": p.unrealized_pnl,
                        "market": p.market, "leverage": p.leverage,
                    })
            except Exception as e:  # noqa: BLE001
                errors[name] = str(e)[:150]
                log.warning("Portfolio fetch failed for %s: %s", name, str(e)[:150])
        SharedState.set_portfolio(total_eq, total_cash, positions, "execution")
        alerts = _reconcile_positions(exchanges, positions)
        for a in alerts:
            log.warning("EXIT-CHECK: %s", a)

        hb.status = "error" if errors and total_eq == 0 else "ok"
        hb.message = (f"open={len(final_open)} submitted={len(new_orders)} "
                      f"fills={len(newly_filled)} equity={total_eq:.2f}")
        hb.details = {"skipped": skipped, "requeued": len(requeue), "broker_errors": errors,
                      "exit_alerts": alerts[:5], "dry_run": cfg.DRY_RUN}
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("Execution failed: %s", e)
        _track_error(str(e))
        hb.status, hb.message = "error", str(e)[:200]
        return 1
    finally:
        hb.last_run = now_stamp()
        hb.duration_s = time.time() - t0
        hb.write()


if __name__ == "__main__":
    sys.exit(run())
