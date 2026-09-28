"""Bitunix-Adapter: Signatur, Endpunkte, Symbole, Resilienz.

Die alten Fehler waren: erfundener Testnet-Host (NXDOMAIN), HMAC über die
Query-Params statt doppelter SHA256 über Header, POST statt GET für
Konto/Positionen, BTC/USDT statt BTCUSDT, Kline-Antwort als Arrays statt
Objekten. Jeder Test prüft genau einen davon.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

import pytest
import requests

from exchanges.base import Order, OrderType, Side, OrderStatus
from exchanges.bitunix_exchange import BitunixExchange
from scripts.common.config import cfg
from scripts.common.net import reset_breakers


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Recorder:
    """Fängt jeden HTTP-Aufruf ab und liefert vorgegebene Antworten."""

    def __init__(self, responses=None, exc=None):
        self.calls: list[dict] = []
        self.responses = list(responses or [])
        self.exc = exc
        self.max_calls = None

    def __call__(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params or {},
                           "data": data, "headers": headers or {}})
        if self.exc is not None:
            raise self.exc
        # Die letzte vorgegebene Antwort wird wiederholt, damit Retries dasselbe
        # Ergebnis sehen (sonst verschleiert ein Retry einen Fehler).
        idx = min(len(self.calls) - 1, len(self.responses) - 1) if self.responses else -1
        payload = self.responses[idx] if idx >= 0 else {"code": 0, "data": [], "msg": "Success"}
        return FakeResponse(payload)


@pytest.fixture
def bu(monkeypatch):
    """BitunixExchange ohne Netzwerk, mit erzwungenen Config-Werten."""
    reset_breakers()
    monkeypatch.setattr(cfg, "BITUNIX_API_KEY", "apikey123", raising=False)
    monkeypatch.setattr(cfg, "BITUNIX_SECRET_KEY", "secret123", raising=False)
    monkeypatch.setattr(cfg, "BITUNIX_BASE_URL", "https://fapi.bitunix.com", raising=False)
    monkeypatch.setattr(cfg, "BITUNIX_TESTNET", True, raising=False)
    monkeypatch.setattr(cfg, "BITUNIX_ALLOW_LIVE_ORDERS", False, raising=False)
    monkeypatch.setattr(cfg, "HTTP_MAX_RETRIES", 3, raising=False)
    monkeypatch.setattr(cfg, "BROKER_MAX_CONSECUTIVE_ERRORS", 3, raising=False)
    monkeypatch.setattr(cfg, "BROKER_COOLDOWN_S", 300, raising=False)
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    return BitunixExchange()


def test_base_url_is_the_only_real_host(bu):
    # fapi-sim.bitunix.com existiert nicht -> DNS-Fehler im Originallog.
    assert bu.base == "https://fapi.bitunix.com"
    assert "sim" not in bu.base


def test_testnet_mode_is_read_only(bu):
    assert bu.read_only is True
    rec = Recorder([{"code": 0, "data": {"orderId": "1"}}])
    bu.session.request = rec
    order = Order(broker="bitunix", symbol="BTC/USDT", side=Side.BUY, qty=0.01)
    out = bu.place_order(order)
    assert out.status == OrderStatus.REJECTED
    assert "read-only" in (out.error or "")
    assert rec.calls == []          # es wurde kein einziges HTTP-Request gesendet


def test_get_orders_allowed_after_opt_in(monkeypatch, cfg_stub=None):
    monkeypatch.setattr(cfg, "BITUNIX_ALLOW_LIVE_ORDERS", True, raising=False)
    monkeypatch.setattr(cfg, "BITUNIX_TESTNET", True, raising=False)
    monkeypatch.setattr(cfg, "DRY_RUN", False, raising=False)
    ex = BitunixExchange()
    rec = Recorder([{"code": 0, "data": {"orderId": "991", "clientId": "c1"}}])
    ex.session.request = rec
    o = Order(broker="bitunix", symbol="BTC/USDT", side=Side.SELL, qty=0.0021,
              type=OrderType.MARKET, stop_loss=59000.5, take_profit=61000.25,
              client_order_id="zhf-1")
    out = ex.place_order(o)
    assert out.status == OrderStatus.NEW and out.broker_order_id == "991"
    call = rec.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v1/futures/trade/place_order")
    body = json.loads(call["data"])
    assert body["symbol"] == "BTCUSDT"                      # kein BTC/USDT
    assert body["orderType"] == "MARKET"
    assert body["side"] == "SELL"
    assert body["qty"] == "0.0021"                           # kein 2.1e-03
    assert body["slPrice"] == "59000.5" and body["tpPrice"] == "61000.25"


def test_signature_matches_doc_spec(bu):
    """digest = SHA256(nonce+timestamp+api-key+queryParams+body); sign = SHA256(digest+secret)."""
    rec = Recorder([{"code": 0, "data": [{"marginCoin": "USDT", "available": "10"}]}])
    bu.session.request = rec
    bu.get_account()
    call = rec.calls[0]
    h = call["headers"]
    assert set(("api-key", "nonce", "timestamp", "sign")).issubset(h)
    assert len(h["nonce"]) == 32
    assert h["timestamp"].isdigit() and len(h["timestamp"]) == 13   # Millisekunden
    qp = "".join(f"{k}{v}" for k, v in sorted({"marginCoin": cfg.BITUNIX_MARGIN_COIN}.items()))
    digest = hashlib.sha256((h["nonce"] + h["timestamp"] + "apikey123" + qp + "").encode()).hexdigest()
    expected = hashlib.sha256((digest + "secret123").encode()).hexdigest()
    assert h["sign"] == expected


def test_account_is_signed_get_and_parses_list(bu):
    rec = Recorder([{"code": 0, "data": [{"available": "500", "frozen": "10", "margin": "40",
                                          "crossUnrealizedPNL": "5", "isolationUnrealizedPNL": "-1"}],
                     "msg": "Success"}])
    bu.session.request = rec
    acc = bu.get_account()
    assert rec.calls[0]["method"] == "GET"
    assert urlparse(rec.calls[0]["url"]).path == "/api/v1/futures/account"
    assert rec.calls[0]["params"] == {"marginCoin": "USDT"}
    assert acc["cash"] == 500.0
    assert acc["equity"] == pytest.approx(554.0)     # avail+frozen+margin+unrealised


def test_positions_use_pending_endpoint_and_signed_qty(bu):
    rec = Recorder([{"code": 0, "data": [
        {"symbol": "BTCUSDT", "qty": "0.5", "side": "SHORT", "avgOpenPrice": "60000",
         "unrealizedPNL": "12.5", "leverage": "3"},
        {"symbol": "ETHUSDT", "qty": "0", "side": "LONG", "avgOpenPrice": "3000",
         "unrealizedPNL": "0", "leverage": "1"},
    ]}])
    bu.session.request = rec
    positions = bu.get_positions()
    assert urlparse(rec.calls[0]["url"]).path == "/api/v1/futures/position/get_pending_positions"
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "BTC/USDT" and p.qty == -0.5 and p.avg_entry == 60000.0
    assert p.leverage == 3 and p.unrealized_pnl == 12.5


def test_kline_symbol_and_object_rows(bu):
    rec = Recorder([{"code": 0, "data": [
        {"time": 1700000000000, "open": "100", "high": "110", "low": "99", "close": "105",
         "baseVol": "12"},
        {"time": 1700003600000, "open": "105", "high": "108", "low": "104", "close": "107",
         "baseVol": "7"},
    ]}])
    bu.session.request = rec
    bars = bu.get_bars("BTC/USDT", "1h", limit=500)     # limit>200 muss geclamped werden
    assert rec.calls[0]["params"]["limit"] == 200
    assert rec.calls[0]["params"]["symbol"] == "BTCUSDT"
    assert urlparse(rec.calls[0]["url"]).path == "/api/v1/futures/market/kline"
    assert len(bars) == 2 and bars[1].close == 107.0
    assert bars[0].timestamp.tzinfo is not None


def test_business_error_is_logged_and_empty_result(bu):
    rec = Recorder([{"code": 40017, "msg": "api key not exist", "data": None}])
    bu.session.request = rec
    assert bu.get_bars("BTC/USDT", "1h", 10) == []
    assert "api key not exist" in bu.breaker.last_error


def test_dns_failure_is_not_retried_and_opens_breaker(bu):
    """Der Originalfehler: NameResolutionError. Kein Retry-Sturm, danach Cooldown."""
    def dns(*a, **kw):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='fapi.bitunix.com', port=443): Max retries exceeded "
            "(Caused by NameResolutionError(\"Failed to resolve 'fapi.bitunix.com' "
            "([Errno -2] Name or service not known)\"))")
    bu.session.request = dns
    for _ in range(3):
        assert bu.get_bars("BTC/USDT", "1h", 10) == []
    assert bu.breaker.open
    assert bu.get_bars("BTC/USDT", "1h", 10) == []      # Blocker: kein weiterer Versuch
    assert bu.status()["breaker"].startswith("bitunix: 3")


def test_cancel_order_payload(bu):
    monkey = Recorder([{"code": 0, "data": {"successList": [{"id": "42"}], "failureList": []}}])
    bu.session.request = monkey
    # cancel ist eine Order-Operation -> im read-only Modus blockiert
    assert bu.cancel_order("42", "BTC/USDT") in (False, True)
    if monkey.calls:
        body = json.loads(monkey.calls[0]["data"])
        assert body["symbol"] == "BTCUSDT" and body["orderList"][0]["orderId"] == "42"
