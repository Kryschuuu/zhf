"""Research-Agent: Daten-Gate, Anti-Halluzination, Watchlist-Hygiene.

Der kritischste Fix: ohne Marktdaten durfte das LLM gar nicht erst befragt
werden. Vorher landeten bei 0 Kerzen trotzdem "Kandidaten" im State – erfundene
Trades. Ausserdem: identische Symbole auf zwei Börsen (BTC/USDT bei BingX *und*
Bitunix) überschrieben sich im Feature-Dictionary.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.research.run as research
from scripts.common.config import cfg
from scripts.common.state import SharedState
from tests.stubs import StubExchange, make_bars


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    wl = tmp_path / "strategies"
    wl.mkdir()
    monkeypatch.setattr(cfg, "STRATEGIES_DIR", wl, raising=False)
    for f in (SharedState.CANDIDATES, SharedState.VALIDATED, SharedState.APPROVED_TRADES,
              SharedState.MARKET_DATA_STATUS, SharedState.KILLSWITCH):
        f.unlink(missing_ok=True)
    return wl


def write_watchlist(dir_path: Path, entries: list[dict]) -> None:
    (dir_path / "watchlist.json").write_text(json.dumps(entries))


def patch_llm(monkeypatch, calls: list, result: dict | None = None, exc: Exception | None = None):
    def fake(prompt, system_prompt=None, agent="default", temperature=0.3):
        calls.append({"prompt": prompt, "agent": agent})
        if exc:
            raise exc
        return result or {}
    monkeypatch.setattr(research, "call_llm_json", fake)


def test_no_market_data_means_no_candidates_and_no_llm(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls)
    stub = StubExchange(bars=[])
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    write_watchlist(clean_state, [{"symbol": "SPY", "broker": "alpaca", "market": "stocks",
                                   "timeframe": "1h"}])
    rc = research.run()
    assert rc == 1
    assert calls == [], "LLM darf ohne Daten nicht gefragt werden"
    assert SharedState.candidates() == []
    md = SharedState.market_data_status()
    assert md["assets_no_data"] == 1 and md["state"] == "degraded"


def test_hallucinated_symbols_are_filtered_out(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls, {"candidates": [
        {"symbol": "MOON/USDT", "broker": "bingx", "market": "crypto_perp", "strategy": "x",
         "direction": "LONG", "confidence": 0.99, "stop_loss_pct": 1.0, "take_profit_pct": 5.0},
        {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "strategy": "ema_crossover",
         "direction": "LONG", "confidence": 0.7, "stop_loss_pct": 1.2, "take_profit_pct": 3.0},
    ]})
    stub = StubExchange(bars=make_bars(120, trend=-0.004, noise=0.02))
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    write_watchlist(clean_state, [{"symbol": "SPY", "broker": "alpaca", "market": "stocks",
                                   "timeframe": "1h"}])
    assert research.run() == 0
    cands = SharedState.candidates()
    assert len(cands) == 1 and cands[0]["symbol"] == "SPY"
    assert cands[0]["source"] == "llm"


def test_llm_stop_loss_is_capped_and_rr_enforced(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls, {"candidates": [
        {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "strategy": "mean_reversion",
         "direction": "LONG", "confidence": 0.8, "stop_loss_pct": 9.0, "take_profit_pct": 1.0},
    ]})
    stub = StubExchange(bars=make_bars(120, trend=-0.004, noise=0.02))
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    write_watchlist(clean_state, [{"symbol": "SPY", "broker": "alpaca", "market": "stocks",
                                   "timeframe": "1h"}])
    research.run()
    c = SharedState.candidates()[0]
    assert c["stop_loss_pct"] <= 2.0 and c["take_profit_pct"] >= 2 * c["stop_loss_pct"]


def test_short_on_stocks_is_dropped(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls, {"candidates": [
        {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "strategy": "x",
         "direction": "SHORT", "confidence": 0.9, "stop_loss_pct": 1.0, "take_profit_pct": 3.0},
    ]})
    stub = StubExchange(bars=make_bars(120, trend=0.001))
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    write_watchlist(clean_state, [{"symbol": "SPY", "broker": "alpaca", "market": "stocks",
                                   "timeframe": "1h"}])
    research.run()
    assert SharedState.candidates() == []


def test_same_symbol_on_two_brokers_does_not_collide(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls)
    bars = make_bars(150, trend=-0.003, noise=0.03, symbol="BTC/USDT")
    bingx = StubExchange(bars=bars)
    bitunix = StubExchange(bars=bars)
    monkeypatch.setattr(research, "get_exchanges", lambda: {"bingx": bingx, "bitunix": bitunix})
    write_watchlist(clean_state, [
        {"symbol": "BTC/USDT", "broker": "bingx", "market": "crypto_perp", "timeframe": "1h"},
        {"symbol": "BTC/USDT", "broker": "bitunix", "market": "crypto_perp", "timeframe": "1h"},
    ])
    assert research.run() == 0
    md = SharedState.market_data_status()
    assert md["assets_ok"] == 2                      # beide Assets gezählt (alt: 1)


def test_watchlist_skips_unknown_broker_and_invalid_symbols(clean_state, monkeypatch):
    stub = StubExchange()
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    monkeypatch.setattr(research, "unavailable_brokers", lambda: {"bingx": "keine Keys"})
    write_watchlist(clean_state, [
        {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
        {"symbol": "SYNTH_A", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
        {"symbol": "BTC/USDT", "broker": "bingx", "market": "crypto_perp", "timeframe": "1h"},
        {"symbol": "SPY", "broker": "alpaca", "market": "stocks", "timeframe": "1h"},
    ])
    wl, skipped = research.load_watchlist({"alpaca": stub})
    assert [w["symbol"] for w in wl] == ["SPY"]
    reasons = {s["reason"] for s in skipped}
    assert any("nicht geladen" in r for r in reasons)
    assert any("ungültig" in r for r in reasons)
    assert any("Duplikat" in r for r in reasons)


def test_killswitch_stops_research(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls)
    SharedState.activate_killswitch("test")
    stub = StubExchange()
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    assert research.run() == 0
    assert calls == []
    assert stub.bar_calls == []


def test_bars_exception_is_counted_not_fatal(clean_state, monkeypatch):
    calls: list = []
    patch_llm(monkeypatch, calls)
    stub = StubExchange(raise_on_bars=RuntimeError("boom"))
    monkeypatch.setattr(research, "get_exchanges", lambda: {"alpaca": stub})
    write_watchlist(clean_state, [{"symbol": "SPY", "broker": "alpaca", "market": "stocks",
                                   "timeframe": "1h"}])
    assert research.run() == 1
    assert SharedState.market_data_status()["assets_error"] == 1
