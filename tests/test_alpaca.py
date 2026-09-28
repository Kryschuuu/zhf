"""Alpaca-Adapter: Feed-Fallback (Free-Plan), Bar-Bereinigung, Order-Bau.

Der Log zeigte ``Alpaca get_bars(SPY) failed: subscription does not permit
querying recent SIP data`` – Ursache: der Code hat den Standard-Feed (SIP)
abgefragt, der ohne bezahltes Abo die letzten 15 min sperrt. Ausserdem:
``invalid symbol: SYNTH_A`` (Platzhalter-Symbole aus dem Synth-Test wurden an
den echten Broker geschickt).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from exchanges.alpaca_exchange import AlpacaExchange
from exchanges.base import Order, OrderStatus, OrderType, Side
from scripts.common.config import cfg
from scripts.common.net import get_breaker, reset_breakers


class APIError(Exception):
    """Nachbau des alpaca-py Fehlers (gleiche Text-Bausteine)."""


class StubData:
    def __init__(self, feeds_ok=("iex",), raise_for=(), n_bars=50, partial_last=False,
                 empty=False):
        self.feeds_ok = set(feeds_ok)
        self.raise_for = set(raise_for)
        self.n_bars = n_bars
        self.partial_last = partial_last
        self.empty = empty
        self.requests: list = []

    def get_stock_bars(self, req):
        return self._handle(req, "stock")

    def get_crypto_bars(self, req):
        return self._handle(req, "crypto")

    def _handle(self, req, kind):
        feed = getattr(req, "feed", None)
        feed = getattr(feed, "value", feed) or ""
        self.requests.append({"req": req, "feed": feed, "kind": kind})
        if feed in self.raise_for or kind in self.raise_for:
            raise APIError('{"message":"subscription does not permit querying recent SIP data"}')
        if self.empty:
            return type("R", (), {"df": pd.DataFrame()})()
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        rows = []
        n = self.n_bars
        for i in range(n):
            ts = now - timedelta(hours=n - 1 - i)
            if self.partial_last and i == n - 1:
                ts = datetime.now(timezone.utc) - timedelta(minutes=4)
            px = 100 + i
            rows.append({"timestamp": ts, "open": px, "high": px + 1, "low": px - 1,
                         "close": px + 0.5, "volume": 1000 + i})
        df = pd.DataFrame(rows).set_index([pd.Index(["SPY"] * n), "timestamp"])
        return type("R", (), {"df": df})()


class StubClock:
    def __init__(self, is_open=True):
        self.is_open = is_open
        self.calls = 0

    def get_clock(self):
        self.calls += 1
        return type("C", (), {"is_open": self.is_open})()


class StubTrading:
    def __init__(self, submit_error: str | None = None, equity="100000", cash="100000"):
        self.submit_error = submit_error
        self.submitted: list = []
        self.equity, self.cash = equity, cash
        self.clock = StubClock()

    def get_account(self):
        return type("A", (), {"equity": self.equity, "cash": self.cash,
                              "buying_power": self.equity, "currency": "USD"})()

    def get_all_positions(self):
        return []

    def get_all_assets(self):
        raise APIError("assets endpoint not available in test")

    def submit_order(self, req):
        self.submitted.append(req)
        if self.submit_error:
            if "bracket" in self.submit_error and getattr(req, "order_class", None) is not None:
                raise APIError(self.submit_error)
            if "bracket" not in self.submit_error:
                raise APIError(self.submit_error)
        return type("R", (), {"id": "oid-1"})()

    def get_clock(self):
        return self.clock.get_clock()


def make(stock=StubData, crypto=None, trading=None, monkeypatch_feed="auto"):
    reset_breakers()
    ex = object.__new__(AlpacaExchange)
    ex.client = trading or StubTrading()
    ex.stock_data = stock() if callable(stock) else stock
    ex.crypto_data = crypto or StubData()
    ex.breaker = get_breaker("alpaca")
    ex._feed_working = None
    ex._feed_broken = set()
    ex._clock_cache = (0.0, False)
    ex._assets_cache = (0.0, set())
    ex._warned_symbols = set()
    return ex


def test_default_feed_is_iex_for_free_plan(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_DATA_FEED", "auto", raising=False)
    ex = make()
    bars = ex.get_bars("SPY", "1h", limit=60)
    assert bars, "Erwartet Kerzen über den IEX-Feed"
    assert ex.stock_data.requests[0]["feed"] in ("iex", None)
    used = {r["feed"] for r in ex.stock_data.requests}
    assert "sip" not in used


def test_subscription_error_falls_back_to_next_feed(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_DATA_FEED", "auto", raising=False)
    ex = make(stock=lambda: StubData(feeds_ok=("delayed_sip",), raise_for={"iex"}))
    bars = ex.get_bars("SPY", "1h", limit=60)
    assert len(bars) > 0
    assert "iex" in ex._feed_broken                       # gemerkt: nicht nochmal versuchen
    assert ex.stock_data.requests[-1]["feed"] == "delayed_sip"


def test_sip_only_when_configured(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_DATA_FEED", "sip", raising=False)
    ex = make()
    ex.get_bars("SPY", "1h", limit=60)
    assert ex.stock_data.requests[0]["feed"] == "sip"


def test_placeholder_symbol_never_hits_the_api():
    ex = make()
    for bad in ("SYNTH_A", "SYNTH_B", "SYNTH_ETH"):
        assert ex.get_bars(bad, "1h", 10) == []
    assert ex.stock_data.requests == []                    # 0 Netzwerk-Aufrufe


def test_partial_last_bar_dropped_and_bars_sorted(monkeypatch):
    monkeypatch.setattr(cfg, "ALPACA_DROP_PARTIAL_BAR", True, raising=False)
    ex = make(stock=lambda: StubData(partial_last=True))
    bars = ex.get_bars("SPY", "1h", limit=50)
    assert bars[-1].timestamp < datetime.now(timezone.utc) - timedelta(minutes=30)
    stamps = [b.timestamp for b in bars]
    assert stamps == sorted(stamps)


def test_crypto_bars_use_crypto_client():
    ex = make()
    bars = ex.get_bars("BTC/USD", "1h", limit=40)
    assert bars
    assert ex.crypto_data.requests and ex.crypto_data.requests[0]["kind"] == "crypto"


def test_dry_run_fill_has_price_not_zero(monkeypatch):
    """Alter Bug: DRY_RUN setzte avg_fill_price=0 → notional_usd=0 → Cost-Agent blind."""
    monkeypatch.setattr(cfg, "DRY_RUN", True, raising=False)
    ex = make()
    o = Order(broker="alpaca", symbol="AAPL", side=Side.BUY, qty=3,
              entry_reference_price=198.5)
    out = ex.place_order(o)
    assert out.status == OrderStatus.FILLED
    assert out.avg_fill_price == pytest.approx(198.5)
    assert out.broker_order_id.startswith("dry-")
    assert ex.client.submitted == []                       # nichts an den Broker gesendet


def test_crypto_market_order_uses_ioc_not_day(monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    ex = make()
    o = Order(broker="alpaca", symbol="BTC/USD", side=Side.BUY, qty=0.002,
              type=OrderType.MARKET, client_order_id="c1")
    out = ex.place_order(o)
    assert out.status == OrderStatus.NEW
    req = ex.client.submitted[0]
    assert req.time_in_force.value == "ioc"
    assert req.qty == pytest.approx(0.002)
    assert getattr(req, "notional", None) is None          # kein Dollar-Betrag als "qty"


def test_fractional_stock_qty_stays_fractional(monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    ex = make()
    o = Order(broker="alpaca", symbol="AAPL", side=Side.BUY, qty=0.31,
              type=OrderType.MARKET, client_order_id="c2")
    assert ex.place_order(o).status == OrderStatus.NEW
    req = ex.client.submitted[0]
    assert req.qty == pytest.approx(0.31)
    assert getattr(req, "notional", None) is None


def test_too_small_order_is_rejected_with_reason(monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    ex = make()
    o = Order(broker="alpaca", symbol="AAPL", side=Side.BUY, qty=0.0, client_order_id="c3")
    out = ex.place_order(o)
    assert out.status == OrderStatus.REJECTED
    assert "too small" in out.error


def test_bracket_oto_attached_and_falls_back(monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    monkeypatch.setattr(cfg, "ALPACA_BRACKET_ORDERS", True, raising=False)
    ex = make()
    o = Order(broker="alpaca", symbol="AAPL", side=Side.BUY, qty=5, type=OrderType.MARKET,
              stop_loss=95.0, take_profit=110.0, client_order_id="c4")
    out = ex.place_order(o)
    req = ex.client.submitted[0]
    assert str(req.order_class.value) == "oto"
    assert float(req.take_profit.limit_price) == 110.0
    assert float(getattr(req.stop_loss, "stop_price", None)
                 or req.stop_loss.stop_loss_price) == 95.0
    assert out.status == OrderStatus.NEW

    # Broker kennt OTO nicht → Retry ohne Bracket, aber Order läuft trotzdem
    ex2 = make(trading=StubTrading(submit_error="order_class bracket not supported"))
    o2 = Order(broker="alpaca", symbol="AAPL", side=Side.BUY, qty=5, type=OrderType.MARKET,
               stop_loss=95.0, take_profit=110.0, client_order_id="c5")
    out2 = ex2.place_order(o2)
    assert out2.status == OrderStatus.NEW
    assert len(ex2.client.submitted) == 2
    assert getattr(ex2.client.submitted[-1], "order_class", None) is None
    assert "bracket" in (out2.error or "")


def test_market_open_clock_is_cached():
    ex = make()
    assert ex.is_market_open("AAPL") is True
    assert ex.is_market_open("MSFT") is True
    assert ex.client.clock.calls == 1                      # 1 Call statt 1 pro Symbol
    assert ex.is_market_open("BTC/USD") is True            # Krypto immer offen, kein Call
