"""Infrastruktur: Fee-Schedule, Retry/Backoff/Circuit-Breaker, Pfad-Isolation, Watchdog."""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from scripts.common import net
from scripts.common.config import cfg
from pathlib import Path


# ------------------------------------------------------------------ fees
def test_fee_schedule_matches_public_rates():
    from scripts.common import fees as F

    assert F.taker_fee("alpaca", "stocks") == 0.0
    assert F.taker_fee("alpaca", "crypto") == pytest.approx(0.0025)
    assert F.maker_fee("alpaca", "crypto") == pytest.approx(0.0015)
    assert F.taker_fee("bingx", "crypto_perp") == pytest.approx(0.0005)
    assert F.taker_fee("bitunix", "crypto_perp") == pytest.approx(0.0006)
    assert F.round_trip_fee("bitunix", "crypto_perp") == pytest.approx(0.0012)
    # Unbekanntes Paar -> neutrale 0.1 %, nicht 0 (kostenlos ist nie)
    assert F.taker_fee("irgendwas", "stocks") == pytest.approx(0.001)


def test_breakeven_move_is_not_divided_by_leverage():
    from scripts.common.fees import breakeven_move_pct
    # Gebühren fallen auf dem vollen Notional an -> Hebel ändert den Break-even nicht.
    assert breakeven_move_pct("bingx", "crypto_perp") == pytest.approx(0.1)


def test_fee_schedule_env_override(monkeypatch):
    monkeypatch.setenv("FEE_SCHEDULE_JSON", '{"bingx": {"crypto_perp": [0.0002, 0.0004]}}')
    F = importlib.reload(importlib.import_module("scripts.common.fees"))
    assert F.taker_fee("bingx", "crypto_perp") == pytest.approx(0.0004)
    monkeypatch.delenv("FEE_SCHEDULE_JSON")
    importlib.reload(F)


def test_expected_costs_includes_perp_funding():
    from scripts.common.fees import expected_costs
    c = expected_costs(10_000, "bingx", "crypto_perp", hold_hours=8)
    assert c["commission_usd"] == pytest.approx(10_000 * 0.0005 * 2, rel=1e-6)
    assert c["funding_usd"] > 0
    s = expected_costs(10_000, "alpaca", "stocks", hold_hours=8)
    assert s["funding_usd"] == 0.0


# ------------------------------------------------------------------ net
def test_dns_error_is_classified_and_not_retried():
    err = requests.ConnectionError(
        "HTTPSConnectionPool(host='fapi-sim.bitunix.com', port=443): Max retries exceeded "
        "(Caused by NameResolutionError(\"Failed to resolve 'fapi-sim.bitunix.com' "
        "([Errno -2] Name or service not known)\"))")
    assert net.classify_error(err) == "dns"
    assert net.is_transient(err) is False
    calls = []

    def boom():
        calls.append(1)
        raise err
    with pytest.raises(requests.ConnectionError):
        net.retry_call(boom, attempts=5, base_delay=0.001)
    assert len(calls) == 1            # ein Versuch, kein Retry-Sturm


def test_rate_limit_is_retried_with_backoff():
    class E(requests.HTTPError):
        status_code = 429

    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise E("429 too many requests")
        return "ok"
    assert net.retry_call(flaky, attempts=4, base_delay=0.001) == "ok"
    assert state["n"] == 3


def test_breaker_opens_and_half_opens():
    net.reset_breakers()
    b = net.get_breaker("unit-test")
    b.max_failures = 2
    b.cooldown_s = 60
    assert b.allow()[0]
    b.record_failure("boom1")
    b.record_failure("boom2")
    assert b.open
    allowed, why = b.allow()
    assert not allowed and "cooldown" in why
    b.opened_at = datetime.now(timezone.utc).timestamp() - 120   # abgelaufen
    allowed, why = b.allow()
    assert allowed and why == "half-open"
    b.record_success()
    assert not b.open and b.failures == 0
    net.reset_breakers()


def test_throttle_respects_interval(monkeypatch):
    slept = []
    monkeypatch.setattr(net.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(net.time, "monotonic", lambda: 0.0)
    net.throttle("x", 0.5)
    net.throttle("x", 0.5)
    assert slept and slept[0] == pytest.approx(0.5)


# ------------------------------------------------------------------ config/pfade
def test_data_dir_isolation_moves_all_runtime_paths(monkeypatch):
    """ZHF_DATA_DIR muss ALLES umziehen – sonst schreibt ein Test in den Live-State."""
    import os
    import scripts.common.config as C
    previous = os.environ.get("ZHF_DATA_DIR")
    monkeypatch.setenv("ZHF_DATA_DIR", "/tmp/zhf-isolated")
    importlib.reload(C)
    try:
        base = C.Path("/tmp/zhf-isolated")
        assert C.cfg.DATA_DIR == base
        assert C.cfg.LOG_DIR == base / "logs"
        assert C.cfg.REPORTS_DIR == base / "reports"
        assert (C.cfg.DATA_DIR / "signals").exists()
        assert (C.cfg.DATA_DIR / "heartbeats").exists()
    finally:
        # Modulzustand für die anderen Tests wiederherstellen (cfg ist global!).
        if previous:
            os.environ["ZHF_DATA_DIR"] = previous
        else:
            monkeypatch.delenv("ZHF_DATA_DIR", raising=False)
        importlib.reload(C)
        assert str(C.cfg.DATA_DIR) == previous or previous is None


def test_redacted_config_has_no_secrets():
    d = cfg.redacted()
    assert not any("KEY" in k.upper() for k in d)
    assert d["DRY_RUN"] in (True, False)


# ------------------------------------------------------------------ state
def test_candidates_accepts_both_shapes(tmp_path):
    from scripts.common.state import SharedState
    SharedState.CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    SharedState.CANDIDATES.write_text(json.dumps([{"symbol": "SPY"}]))
    assert SharedState.candidates() == [{"symbol": "SPY"}]
    SharedState.CANDIDATES.write_text(json.dumps({"generated_at": "x", "candidates": [{"symbol": "QQQ"}]}))
    assert SharedState.candidates() == [{"symbol": "QQQ"}]
    SharedState.CANDIDATES.write_text("{kaputter json")
    assert SharedState.candidates() == []


def test_heartbeat_survives_unknown_fields(tmp_path):
    from scripts.common.state import Heartbeat
    p = cfg.DATA_DIR / "heartbeats" / "futureagent.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"agent": "futureagent", "last_run": "x", "status": "ok",
                             "message": "m", "duration_s": 1, "brand_new_field": 42}))
    hb = Heartbeat.read("futureagent")
    assert hb.status == "ok"
    assert Heartbeat.read("never-ran-agent").status == "never_run"


# ------------------------------------------------------------------ watchdog
def test_watchdog_reports_market_data_gap_and_critical_uses(isolated_stub, monkeypatch):
    from scripts.common.state import SharedState
    import scripts.monitoring.watchdog as wd
    SharedState.set_market_data_status({"state": "degraded", "assets": 10, "assets_ok": 0,
                                        "errors": {"alpaca:SPY": "0 bars"}})
    rc = wd.run()
    assert rc == 1                                    # kritischer Befund
    rep = json.loads((cfg.REPORTS_DIR / "watchdog.json").read_text())
    assert any("Marktdaten" in i["msg"] for i in rep["issues"])
    assert any(i["severity"] == "critical" for i in rep["issues"])
    assert rep["critical_count"] >= 1


@pytest.fixture
def isolated_stub(monkeypatch):
    from tests.stubs import StubExchange, make_bars
    stub = StubExchange(bars=make_bars(60))
    monkeypatch.setattr("exchanges.factory.get_exchanges", lambda: {"alpaca": stub}, raising=False)
    monkeypatch.setattr("scripts.monitoring.watchdog.get_exchanges", lambda: {"alpaca": stub})
    monkeypatch.setattr("scripts.monitoring.watchdog.broker_diagnostics",
                        lambda: {"brokers": {"alpaca": {}}, "unavailable": {}})
    monkeypatch.setattr("scripts.monitoring.watchdog.unavailable_brokers", lambda: {})
    monkeypatch.setattr("scripts.monitoring.watchdog._check_heartbeats", lambda rep, now: None)
    monkeypatch.setattr("scripts.monitoring.watchdog._check_llm", lambda rep: (True, True))
    monkeypatch.setattr("scripts.monitoring.watchdog._check_orders", lambda rep: None)
    return stub


def test_watchdog_all_green_is_not_critical(isolated_stub, monkeypatch):
    from scripts.common.state import SharedState
    import scripts.monitoring.watchdog as wd
    SharedState.set_market_data_status({"state": "ok", "assets": 10, "assets_ok": 10})
    assert wd.run() == 0
    rep = json.loads((cfg.REPORTS_DIR / "watchdog.json").read_text())
    assert rep["ok"] is True


# ------------------------------------------------------------------ CEO
def test_ceo_reads_current_fee_report_shape(isolated_stub, monkeypatch):
    """CEO darf sich nicht auf ein veraltetes Report-Schema abstützen (None im Bericht)."""
    import scripts.ceo.daily as ceo
    from scripts.common.fills import append_fill
    append_fill({"broker": "synth", "symbol": "AAA", "side": "BUY", "qty": 1.0,
                 "avg_price": 100.0, "order_id": "o1", "status": "FILLED",
                 "strategy": "mean_reversion", "notional_usd": 100.0})
    fills = ceo._today_fills()
    assert len(fills) == 1
    w = ceo._auto_adjust_weights(fills)          # 1 Fill, kein Fehler -> keine Änderung
    assert w["mean_reversion"] == 1.0


def test_ceo_degrades_weights_on_repeated_errors(monkeypatch, tmp_path):
    import scripts.ceo.daily as ceo
    bad = [{"status": "REJECTED", "strategy": "breakout"} for _ in range(3)]
    monkeypatch.setattr(ceo.cfg, "STRATEGIES_DIR", tmp_path)
    w = ceo._auto_adjust_weights(bad)
    assert w["breakout"] < 1.0
    assert (tmp_path / "strategy_weights.json").exists()
