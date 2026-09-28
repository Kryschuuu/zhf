"""Risk-Agent: Killswitch, Equity-Plausibilität, Sizing, Ablehnungsgründe.

Die drei wichtigsten Korrekturen:
1. `c['backtest']` → KeyError killte den ganzen Agenten, wenn der Backtest ein
   Feld nicht geliefert hatte (der Log zeigte danach "Risk approved 0 / 3").
2. Korrelation wurde für *alle* offenen Positionen beim Broker des Kandidaten
   abgefragt (BTC/USDT von BingX bei Alpaca nachgeschlagen) und O(n²) Kerzen
   geholt. Jetzt: richtiger Broker je Position + Bar-Cache.
3. Positionsgrösse war doppelt gedeckelt und ignorierte das verfügbare Cash →
   jetzt Risiko-basiert mit klaren Caps, die im Trade-Dokument stehen.
"""
from __future__ import annotations

import json

import pytest

import scripts.risk.run as risk
from scripts.common.config import cfg
from scripts.common.state import SharedState
from tests.stubs import StubExchange, make_bars


def cand(**kw) -> dict:
    base = {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "timeframe": "1h",
            "strategy": "mean_reversion", "direction": "LONG", "confidence": 0.7,
            "stop_loss_pct": 1.5, "take_profit_pct": 3.2, "position_size_pct_hint": 1.0,
            "backtest": {"n_trades": 40, "win_rate": 0.55, "profit_factor": 1.6,
                         "sharpe": 1.4, "max_drawdown_pct": 6.0, "expectancy_per_trade": 12,
                         "passed": True, "reason": "passed"}}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for f in (SharedState.APPROVED_TRADES, SharedState.VALIDATED, SharedState.KILLSWITCH,
              SharedState.RISK_REJECTIONS, SharedState.PORTFOLIO):
        f.unlink(missing_ok=True)
    (cfg.DATA_DIR / "equity_log.jsonl").unlink(missing_ok=True)
    monkeypatch.setattr(cfg, "ALPACA_DATA_MIN_INTERVAL_S", 0, raising=False)
    yield


def patch_ex(monkeypatch, **kw) -> StubExchange:
    stub = StubExchange(bars=make_bars(150, trend=-0.002, noise=0.02), **kw)
    monkeypatch.setattr(risk, "get_exchanges", lambda: {"alpaca": stub})
    return stub


def test_killswitch_blocks_new_approvals(isolated, monkeypatch):
    stub = patch_ex(monkeypatch)
    SharedState.activate_killswitch("manual")
    SharedState.set_validated([cand()])
    assert risk.run() == 0
    assert SharedState.approved() == []
    assert stub.bar_calls == []


def test_zero_equity_is_an_error_not_silent_zero_size(isolated, monkeypatch):
    patch_ex(monkeypatch, equity=0.0, cash=0.0)
    SharedState.set_validated([cand()])
    assert risk.run() == 1
    hb = json.loads((cfg.DATA_DIR / "heartbeats" / "risk.json").read_text())
    assert hb["status"] == "error" and "equity=0" in hb["message"]


def test_missing_backtest_field_does_not_crash_agent(isolated, monkeypatch):
    stub = patch_ex(monkeypatch)
    c = cand()
    del c["backtest"]
    SharedState.set_validated([c])
    assert risk.run() == 0                      # kein KeyError
    rej = SharedState.risk_rejections()
    assert rej and "kein Backtest" in rej[0]["reason"]


def test_failed_backtest_is_rejected(isolated, monkeypatch):
    patch_ex(monkeypatch)
    SharedState.set_validated([cand(backtest={"passed": False, "reason": "n=3<15",
                                              "n_trades": 3, "win_rate": 0.4,
                                              "profit_factor": 0.8, "sharpe": 0.2,
                                              "max_drawdown_pct": 30, "expectancy_per_trade": -5})])
    assert risk.run() == 0
    assert SharedState.approved() == []


def test_placeholder_symbol_rejected_before_any_api_call(isolated, monkeypatch):
    stub = patch_ex(monkeypatch)
    SharedState.set_validated([cand(symbol="SYNTH_A")])
    assert risk.run() == 0
    assert SharedState.approved() == []
    assert stub.bar_calls == []                  # kein nutzloser Data-Call
    rej = SharedState.risk_rejections()
    assert "invalid symbol" in rej[0]["reason"]


def test_read_only_broker_rejects_orders(isolated, monkeypatch):
    stub = patch_ex(monkeypatch, read_only=True)
    SharedState.set_validated([cand()])
    assert risk.run() == 0
    rej = SharedState.risk_rejections()
    assert "read-only" in rej[0]["reason"]


def test_risk_reward_below_two_rejected(isolated, monkeypatch):
    patch_ex(monkeypatch)
    SharedState.set_validated([cand(stop_loss_pct=2.0, take_profit_pct=3.0)])
    risk.run()
    rej = SharedState.risk_rejections()
    assert any("R:R" in r["reason"] for r in rej)


def test_approved_trade_fields_and_sizing_caps(isolated, monkeypatch):
    stub = patch_ex(monkeypatch, equity=1000.0, cash=900.0)
    SharedState.set_validated([cand(position_size_pct_hint=2.0, stop_loss_pct=1.5,
                                     take_profit_pct=3.5)])
    assert risk.run() == 0
    trades = SharedState.approved()
    assert len(trades) == 1
    t = trades[0]
    # Risiko 2% bei 1.5% SL → rechnerisch 133 % Notional; gedeckelt durch
    # MAX_POSITION_NOTIONAL_PCT (20 %) und Cash.
    price = stub.bars[-1].close
    assert t["notional_usd"] <= 1000.0 * cfg.MAX_POSITION_NOTIONAL_PCT / 100 + 1
    assert t["notional_usd"] < 900.0 * 1.5
    assert t["sizing_caps"], "Caps müssen dokumentiert sein"
    assert t["leverage"] == 1                    # Aktien immer 1x
    assert t["stop_loss"] < price < t["take_profit"]
    assert abs(t["risk_usd"] - t["notional_usd"] * 1.5 / 100) < 0.5
    assert t["entry_reference_price"] == pytest.approx(price, rel=1e-6)
    assert t["market"] == "stocks"


def test_correlation_uses_position_broker_and_is_cached(isolated, monkeypatch):
    bars = make_bars(150, trend=-0.002, noise=0.02)
    perp = StubExchange(bars=bars)
    alpaca = StubExchange(bars=bars, positions=[])
    monkeypatch.setattr(risk, "get_exchanges", lambda: {"alpaca": alpaca, "bingx": perp})
    from exchanges.base import Position
    alpaca.positions = [Position(symbol="BTC/USDT", qty=0.1, avg_entry=60000,
                                 market="crypto_perp", broker="bingx")]
    SharedState.set_validated([cand()])
    risk.run()
    # Die BingX-Position darf NICHT bei Alpaca angefragt werden.
    assert all(sym != "BTC/USDT" for sym, *_ in alpaca.bar_calls), alpaca.bar_calls
    assert any(sym == "BTC/USDT" for sym, *_ in perp.bar_calls)
    # 100er-Chains wie vorher (O(n²) Calls) gibt es nicht: max. 1 Call pro Symbol/Tf
    assert len(alpaca.bar_calls) <= 2


def test_high_correlation_blocks_the_trade(isolated, monkeypatch):
    bars = make_bars(150, trend=-0.002, noise=0.02)
    from exchanges.base import Position
    stub = StubExchange(bars=bars, positions=[Position(symbol="QQQ", qty=1, avg_entry=100,
                                                        market="stocks", broker="alpaca")],
                       bars_by_symbol={"QQQ": bars})   # identische Serie => corr 1.0
    monkeypatch.setattr(risk, "get_exchanges", lambda: {"alpaca": stub})
    SharedState.set_validated([cand()])
    risk.run()
    assert SharedState.approved() == []
    rej = SharedState.risk_rejections()
    assert any("correlation" in r["reason"] for r in rej)


def test_max_positions_reached(isolated, monkeypatch):
    from exchanges.base import Position
    stub = patch_ex(monkeypatch)
    stub.positions = [Position(symbol=f"S{i}", qty=1, avg_entry=10, market="stocks",
                               broker="alpaca") for i in range(cfg.MAX_OPEN_POSITIONS)]
    SharedState.set_validated([cand()])
    assert risk.run() == 0
    assert SharedState.approved() == []


def test_daily_drawdown_triggers_killswitch(isolated, monkeypatch):
    stub = patch_ex(monkeypatch, equity=9_000.0, cash=9_000.0)
    (cfg.DATA_DIR / "equity_log.jsonl").write_text(json.dumps(
        {"ts": "2000-01-01T00:00:00", "equity": 10_000.0}) + "\n" + json.dumps(
        {"ts": risk.datetime.now(risk.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "equity": 10_000.0}) + "\n")
    SharedState.set_validated([cand()])
    assert risk.run() == 1
    active, reason = SharedState.killswitch_active()
    assert active and "Daily drawdown" in reason


def test_crash_leaves_approved_list_empty(isolated, monkeypatch):
    patch_ex(monkeypatch)
    SharedState.set_validated([{"symbol": "SPY", "broker": "alpaca"}])   # Müll ohne Felder

    def boom(*a, **kw):
        raise RuntimeError("broker explodiert")
    monkeypatch.setattr(risk, "_aggregate_portfolio", boom)
    assert risk.run() == 1
    assert SharedState.approved() == []
