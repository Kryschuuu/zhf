"""Cost-Optimizer: echte Gebühren + gemessene Slippage statt Pauschalen/Nullen."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.cost.run as cost
from scripts.common.config import cfg
from scripts.common.fees import fees
from scripts.common.state import SharedState


def write_fills(rows: list[dict]) -> None:
    from scripts.common.fills import FILL_COLUMNS
    if cost.FILLS_LOG.exists():
        cost.FILLS_LOG.unlink()
    cost.FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with cost.FILLS_LOG.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FILL_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def isolated():
    for f in (cost.FILLS_LOG, cfg.REPORTS_DIR / "fee_report.json", SharedState.MARKET_DATA_STATUS):
        f.unlink(missing_ok=True)
    yield


def test_fees_and_slippage_are_aggregated(isolated, monkeypatch):
    notional = 1000.0
    # BingX-Perp: 0.05 % Taker pro Seite -> Round-Trip 0.1 % = 1.00 USD
    write_fills([{"timestamp": _now(), "broker": "bingx", "symbol": "BTC/USDT", "side": "BUY",
                  "qty": 0.01, "avg_price": 60000, "order_id": "1", "status": "FILLED",
                  "strategy": "ema", "notional_usd": notional, "ref_price": 59940,
                  "slippage_bps": 10.0, "est_fee_usd": notional * fees("bingx", "crypto_perp").round_trip_taker,
                  "mode": "paper"}])
    assert cost.run() == 0
    rep = json.loads((cfg.REPORTS_DIR / "fee_report.json").read_text())
    f = rep["fees"]
    assert f["trades"] == 1
    assert f["total_fees_usd"] == pytest.approx(1.0, abs=0.01)
    assert f["avg_fee_bps"] == pytest.approx(10.0, abs=0.2)
    assert f["avg_slippage_bps"] == pytest.approx(10.0)
    assert f["by_broker"]["bingx"]["slippage_usd"] == pytest.approx(1.0, abs=0.01)
    assert rep["fees"]["total_cost_usd"] == pytest.approx(2.0, abs=0.05)
    #alpaca-Krypto hätte 0.25 % Taker – die Schedule muss im Report stehen
    assert rep["fee_schedule"]["alpaca"]["crypto"]["taker"] == pytest.approx(0.0025)


def test_missing_fee_column_falls_back_to_schedule(isolated):
    write_fills([{"timestamp": _now(), "broker": "bitunix", "symbol": "BTC/USDT", "side": "BUY",
                  "qty": 0.01, "avg_price": 60000, "order_id": "1", "status": "FILLED",
                  "strategy": "x", "notional_usd": 500.0, "ref_price": "", "slippage_bps": "",
                  "est_fee_usd": "", "mode": "live"}])
    cost.run()
    rep = json.loads((cfg.REPORTS_DIR / "fee_report.json").read_text())
    assert rep["fees"]["total_fees_usd"] == pytest.approx(
        500.0 * fees("bitunix", "crypto_perp").round_trip_taker, abs=0.01)


def test_old_fills_outside_the_window_are_ignored(isolated):
    write_fills([{"timestamp": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
                  "broker": "alpaca", "symbol": "SPY", "side": "BUY", "qty": 1,
                  "avg_price": 100, "order_id": "1", "status": "FILLED", "strategy": "x",
                  "notional_usd": 100, "ref_price": 100, "slippage_bps": 0, "est_fee_usd": 0,
                  "mode": "paper"}])
    cost.run()
    rep = json.loads((cfg.REPORTS_DIR / "fee_report.json").read_text())
    assert rep["fees"]["trades"] == 0


def test_no_fills_does_not_call_the_llm(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(cost, "call_llm", lambda *a, **k: calls.append(1))
    assert cost.run() == 0
    assert calls == []
    rep = json.loads((cfg.REPORTS_DIR / "fee_report.json").read_text())
    assert any("Keine Fills" in r["msg"] for r in rep["recommendations"])


def test_market_data_gap_is_reported_as_cost_problem(isolated):
    SharedState.set_market_data_status({"state": "degraded", "assets": 10, "assets_ok": 2})
    write_fills([{"timestamp": _now(), "broker": "alpaca", "symbol": "SPY", "side": "BUY",
                  "qty": 1, "avg_price": 100, "order_id": "1", "status": "FILLED", "strategy": "x",
                  "notional_usd": 100, "ref_price": 100, "slippage_bps": 1, "est_fee_usd": 0.01,
                  "mode": "paper"}])
    cost.run()
    rep = json.loads((cfg.REPORTS_DIR / "fee_report.json").read_text())
    assert any("Marktdaten" in r["msg"] for r in rep["recommendations"])
    assert rep["market_data"]["assets_ok"] == 2


def test_corrupt_fills_line_is_skipped(isolated):
    cost.FILLS_LOG.parent.mkdir(parents=True, exist_ok=True)
    cost.FILLS_LOG.write_text("timestamp,broker\nnot-a-date,alpaca\n")
    assert cost.run() == 0
    rep = json.loads((cfg.REPORTS_DIR / "fee_report.json").read_text())
    assert rep["fees"]["trades"] == 0
