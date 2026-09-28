"""Execution-Agent: Dry-Run-Fills mit Preis, Retries statt stiller Verwürfe.

Vorher: DRY_RUN setzte `avg_fill_price = 0` → `notional_usd = 0` im Fills-Log →
der Cost-Optimizer rechnete mit leeren Zahlen (und Slippage war hartkodiert 0).
Ausserdem wurde approved.json am Zyklusende immer geleert – ein temporär nicht
verarbeitbarer Trade war damit einfach weg.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.execution.run as execution
from scripts.common.config import cfg
from scripts.common.state import SharedState
from tests.stubs import StubExchange, make_bars


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for f in (SharedState.APPROVED_TRADES, SharedState.ORDER_STATE, SharedState.KILLSWITCH,
              execution.ERROR_TRACK):
        f.unlink(missing_ok=True)
    execution.FILLS_LOG.unlink(missing_ok=True)
    monkeypatch.setattr(cfg, "DRY_RUN", True, raising=False)
    monkeypatch.setattr(cfg, "ALPACA_DATA_MIN_INTERVAL_S", 0, raising=False)
    SharedState.set_approved([])
    yield


def trade(**kw) -> dict:
    base = {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "direction": "LONG",
            "qty": 5.0, "order_type": "MARKET", "leverage": 1,
            "stop_loss": 95.0, "take_profit": 110.0, "entry_reference_price": 100.0,
            "notional_usd": 500.0, "strategy": "mean_reversion"}
    base.update(kw)
    return base


def fill_rows() -> list[dict]:
    if not execution.FILLS_LOG.exists():
        return []
    return list(csv.DictReader(execution.FILLS_LOG.open()))


def test_dry_run_fill_has_price_notional_and_slippage(isolated, monkeypatch):
    stub = StubExchange(bars=make_bars(60), equity=1000.0)
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_approved([trade()])
    assert execution.run() == 0
    rows = fill_rows()
    assert len(rows) == 1
    r = rows[0]
    assert float(r["avg_price"]) > 0
    assert float(r["notional_usd"]) > 0
    assert float(r["ref_price"]) == pytest.approx(100.0)
    assert float(r["slippage_bps"]) == pytest.approx(3.0, abs=0.01)   # stub faked 3 bps
    assert r["mode"] == "dry_run"
    assert float(r["est_fee_usd"]) >= 0
    state = SharedState.order_state()
    assert len(state["fills"]) == 1
    assert SharedState.approved() == []          # abgearbeitet


def test_stop_loss_and_take_profit_reach_the_broker(isolated, monkeypatch):
    stub = StubExchange(bars=make_bars(60))
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_approved([trade()])
    execution.run()
    o = stub.orders[0]
    assert (o.stop_loss, o.take_profit) == (95.0, 110.0)
    assert o.entry_reference_price == 100.0
    assert o.market == "stocks"
    assert o.client_order_id and o.client_order_id.startswith("zhf-")


def test_missing_broker_is_requeued_not_dropped(isolated, monkeypatch):
    """Kern-Fix: "broker unavailable" hiess bisher "Trade kommentarlos vergessen"."""
    stub = StubExchange()
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_approved([trade(broker="bitunix")])
    execution.run()
    pending = SharedState.approved()
    assert len(pending) == 1 and pending[0]["attempts"] == 1
    assert stub.orders == []
    # dritter Versuch: Drop (endlos wiederholen wäre auch keine Lösung)
    SharedState.set_approved([trade(broker="bitunix", attempts=execution.MAX_ORDER_ATTEMPTS - 1)])
    execution.run()
    assert SharedState.approved() == []


def test_read_only_broker_blocks_orders(isolated, monkeypatch):
    stub = StubExchange(read_only=True)
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"bitunix": stub})
    SharedState.set_approved([trade(broker="bitunix")])
    execution.run()
    assert stub.orders == []
    assert len(SharedState.approved()) == 1      # vertagt statt verloren


def test_market_closed_defers_the_order(isolated, monkeypatch):
    stub = StubExchange()
    stub.market_open = False
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_approved([trade()])
    execution.run()
    assert stub.orders == []
    assert len(SharedState.approved()) == 1


def test_zero_qty_is_rejected_with_error_log(isolated, monkeypatch):
    stub = StubExchange()
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_approved([trade(qty=0.0)])
    execution.run()
    assert stub.orders == []
    errors = json.loads(execution.ERROR_TRACK.read_text())
    assert any("invalid qty" in e["msg"] for e in errors)


def test_killswitch_stops_execution(isolated, monkeypatch):
    stub = StubExchange()
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.activate_killswitch("drawdown")
    SharedState.set_approved([trade()])
    assert execution.run() == 0
    assert stub.orders == []
    assert len(SharedState.approved()) == 1      # bleibt liegen, bis Killswitch weg ist


def test_error_window_is_utc_based(isolated, monkeypatch):
    """3 Fehler in 5 min → Killswitch. Alte Fehler (lokal vs. UTC!) zählen nicht."""
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    execution.ERROR_TRACK.parent.mkdir(parents=True, exist_ok=True)
    execution.ERROR_TRACK.write_text(json.dumps(
        [{"ts": old, "msg": "alt1"}, {"ts": old, "msg": "alt2"}]))
    execution._track_error("neu1")
    execution._track_error("neu2")
    assert SharedState.killswitch_active()[0] is False       # 2 alt+neu zählen korrekt
    execution._track_error("neu3")
    active, reason = SharedState.killswitch_active()
    assert active and "execution errors" in reason


def test_open_order_status_is_polled_and_finalised(isolated, monkeypatch):
    stub = StubExchange(bars=make_bars(60))
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    o = execution.Order(broker="alpaca", symbol="SPY", side=execution.Side.BUY, qty=5)
    o.status = execution.OrderStatus.FILLED
    o.broker_order_id = "stub-1"
    o.filled_qty = 5
    o.avg_fill_price = 100.3
    stub.orders.append(o)
    SharedState.update_order_state([{
        "broker": "alpaca", "symbol": "SPY", "side": "BUY", "qty": 5, "type": "MARKET",
        "status": "NEW", "broker_order_id": "stub-1", "client_order_id": "c", "strategy": "s",
        "submitted_at": datetime.now(timezone.utc).isoformat()}], [])
    SharedState.set_approved([])
    execution.run()
    state = SharedState.order_state()
    assert state["orders"] == []
    assert state["fills"][0]["status"] == "FILLED"
    assert float(state["fills"][0]["cost"]["notional_usd"]) > 0


def test_fills_log_schema_change_archives_old_file(isolated, monkeypatch):
    execution.FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
    execution.FILLS_LOG.write_text(
        "timestamp,broker,symbol,side,qty,avg_price,order_id,status,strategy,notional_usd\n"
        "2026-01-01T00:00:00+00:00,alpaca,SPY,BUY,1,100,x,FILLED,s,100\n")
    stub = StubExchange(bars=make_bars(60))
    monkeypatch.setattr(execution, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_approved([trade()])
    execution.run()
    from scripts.common.fills import FILL_COLUMNS
    header = execution.FILLS_LOG.open().readline().strip().split(",")
    assert header == FILL_COLUMNS
    assert len(fill_rows()) == 1        # alter Eintrag wurde separiert, nicht mitgezählt
