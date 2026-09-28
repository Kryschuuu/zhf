"""CCXT-Adapter (BingX): Sandbox-Modus, Perp-Symbolauflösung, Datenpfad.

Der Log tat so, als wäre Testnet aktiv ("bingx: 3544 markets loaded
(testnet=True)"), tatsächlich wurde nur ein wirkungsloser
`sandboxMode`-Paramschlüssel übergeben – ccxt kennt den nicht, richtig ist
`set_sandbox_mode(True)`. Ausserdem erwartet ccxt für lineare Perps
`BTC/USDT:USDT`.
"""
from __future__ import annotations

import pytest

import ccxt
from exchanges.base import Order, OrderStatus, Side
from exchanges.ccxt_exchange import CCXTExchange
from scripts.common.config import cfg
from scripts.common.net import reset_breakers


class FakeExchange:
    """Minimal-Stub im Stil einer ccxt-Klasse."""

    def __init__(self, params):
        self.init_params = params
        self.sandbox = None
        self.calls: list[tuple] = []
        self.has_test_url = True
        self.markets = {"BTC/USDT:USDT": {"active": True, "swap": True},
                        "ETH/USDT:USDT": {"active": True, "swap": True},
                        "BTC/USDT": {"active": True, "spot": True}}
        self.limit = 1000
        self.urls = {"test": "https://test", "api": {"swap": "https://open-api.bingx.com/openApi"}}

    def set_sandbox_mode(self, on):
        if not self.has_test_url:
            raise AttributeError("no test urls")
        self.sandbox = on
        self.urls["api"] = {"swap": "https://open-api-vst.bingx.com/openApi"}

    def load_markets(self, *a, **kw):
        self.calls.append(("load_markets",))
        return self.markets

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None, **kw):
        self.calls.append(("fetch_ohlcv", symbol, timeframe, limit))
        base = 1_700_000_000_000
        return [[base + i * 3_600_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10] for i in range(limit or 3)]

    def fetch_balance(self, *a, **kw):
        self.calls.append(("fetch_balance",))
        return {"USDT": {"total": 1234.5, "free": 900.0}}

    def fetch_positions(self, *a, **kw):
        return [{"symbol": "BTC/USDT:USDT", "contracts": 0.5, "side": "long",
                 "entryPrice": 60000, "unrealizedPnl": 3.5, "leverage": 2},
                {"symbol": "ETH/USDT:USDT", "contracts": 0}]

    def create_order(self, symbol, typ, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, typ, side, amount, price, params))
        return {"id": "bx-1", "status": "open"}

    def fetch_order(self, oid, symbol=None):
        return {"id": oid, "symbol": "BTC/USDT:USDT", "side": "buy", "amount": 0.1,
                "filled": 0.1, "average": 61000.0, "status": "closed"}

    def set_leverage(self, lev, symbol):
        self.calls.append(("set_leverage", lev, symbol))
        return {"leverage": lev}

    def cancel_order(self, oid, symbol=None):
        return {"id": oid, "status": "canceled"}


@pytest.fixture
def patch_ccxt(monkeypatch):
    made: list[FakeExchange] = []

    def factory(params):
        ex = FakeExchange(params)
        if not getattr(factory, "allow_test", True):
            ex.has_test_url = False
        made.append(ex)
        return ex

    monkeypatch.setattr(ccxt, "bingx", factory, raising=True)
    monkeypatch.setattr(ccxt, "bitunix", factory, raising=False)
    monkeypatch.setattr(cfg, "BINGX_TESTNET", True, raising=False)
    monkeypatch.setattr(cfg, "BINGX_API_KEY", "key", raising=False)
    monkeypatch.setattr(cfg, "BINGX_SECRET_KEY", "secret", raising=False)
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    monkeypatch.setattr(cfg, "ALPACA_DATA_MIN_INTERVAL_S", 0, raising=False)
    reset_breakers()
    return made, factory


def make(patch_ccxt, allow_test=True):
    made, factory = patch_ccxt
    factory.allow_test = allow_test
    ex = CCXTExchange("bingx", "key", "secret", testnet=True)
    ex._fake = made[-1]
    return ex


def test_sandbox_mode_is_actually_applied(patch_ccxt):
    ex = make(patch_ccxt)
    assert ex._fake.sandbox is True
    assert ex.testnet_applied is True
    assert ex.block_live is False
    # kein toter "sandboxMode"-Paramschlüssel mehr in der Initialisierung
    assert "sandboxMode" not in ex._fake.init_params
    assert ex._fake.init_params["options"]["defaultType"] == "swap"
    assert "open-api-vst" in ex._fake.urls["api"]["swap"]


def test_live_orders_blocked_when_no_sandbox_available(patch_ccxt):
    made, factory = patch_ccxt
    factory.allow_test = False
    ex = CCXTExchange("bingx", "key", "secret", testnet=True)
    fake = made[-1]
    assert ex.testnet_applied is False and ex.block_live is True
    out = ex.place_order(Order(broker="bingx", symbol="BTC/USDT", side=Side.BUY, qty=0.1))
    assert out.status == OrderStatus.REJECTED
    assert "Sandbox" in out.error
    assert not any(c[0] == "create_order" for c in fake.calls)


def test_public_data_without_keys_is_data_only(patch_ccxt, monkeypatch):
    ex = CCXTExchange("bingx", "", "", testnet=False)
    assert ex.read_only is True
    assert ex.get_account()["error"]
    out = ex.place_order(Order(broker="bingx", symbol="BTC/USDT", side=Side.BUY, qty=0.1))
    assert out.status == OrderStatus.REJECTED


def test_perp_symbol_resolved_to_settle_format(patch_ccxt):
    ex = make(patch_ccxt)
    bars = ex.get_bars("BTC/USDT", "1h", limit=40)
    call = [c for c in ex._fake.calls if c[0] == "fetch_ohlcv"][0]
    assert call[1] == "BTC/USDT:USDT"            # ccxt-Linear-Perp-Symbol
    assert call[2] == "1h" and call[3] == 40
    assert bars and bars[-1].close > 100


def test_unknown_symbol_is_skipped_not_crashing(patch_ccxt):
    ex = make(patch_ccxt)
    assert ex.get_bars("DOGE/USDT", "1h", 30) == []
    assert not any(c[0] == "fetch_ohlcv" for c in ex._fake.calls)
    assert ex.get_bars("SYNTH_A", "1h", 30) == []


def test_account_and_positions(patch_ccxt):
    ex = make(patch_ccxt)
    acc = ex.get_account()
    assert acc["equity"] == pytest.approx(1234.5) and acc["cash"] == pytest.approx(900.0)
    pos = ex.get_positions()
    assert len(pos) == 1 and pos[0].symbol == "BTC/USDT:USDT" and pos[0].leverage == 2


def test_place_order_passes_sl_tp_and_leverage(patch_ccxt):
    ex = make(patch_ccxt)
    o = Order(broker="bingx", symbol="BTC/USDT", side=Side.BUY, qty=0.1,
              stop_loss=59000.0, take_profit=61000.0, leverage=2, market="crypto_perp")
    out = ex.place_order(o)
    assert out.status == OrderStatus.NEW and out.broker_order_id == "bx-1"
    call = [c for c in ex._fake.calls if c[0] == "create_order"][0]
    params = call[6]
    assert params["stopLoss"]["triggerPrice"] == 59000.0
    assert params["takeProfit"]["triggerPrice"] == 61000.0
    assert params["leverage"] == 2
    assert call[1] == "BTC/USDT:USDT"


def test_exchange_missing_in_ccxt_has_clear_error(monkeypatch):
    monkeypatch.setattr(ccxt, "exchanges", [])
    with pytest.raises(ImportError) as e:
        CCXTExchange("noboery", "k", "s", testnet=False)
    assert "ccxt" in str(e.value)
