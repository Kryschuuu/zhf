"""Bitunix-Adapter (USDT-Perps) – direkte REST-API, da Bitunix nicht in CCXT existiert.

Dokumentation: https://www.bitunix.com/api-docs/futures/common/introduction.html

Korrekturen gegenüber der alten Implementierung:

1.  **Endpoint**: ``fapi-sim.bitunix.com`` existiert nicht (NXDOMAIN). Bitunix
    betreibt genau *einen* REST-Host ``https://fapi.bitunix.com`` und kein
    öffentliches API-Testnet ("Demo Trading" gibt es nur im Web-UI, ohne API).
    ``BITUNIX_TESTNET=true`` schaltet deshalb auf **read-only**: Marktdaten ja,
    Orders werden hart blockiert, bis ``BITUNIX_ALLOW_LIVE_ORDERS=true`` gesetzt
    ist. So kann niemand versehentlich "im Glauben an ein Sim-Konto" echt traden.
2.  **Signatur**: alt war HMAC-SHA256 über den Query-String in den *Params*.
    Vorgeschrieben ist eine doppelte SHA256 über
    ``nonce + timestamp + api-key + queryParams + body`` und die Übergabe über
    Header (``api-key``, ``nonce`` (32), ``timestamp`` (ms), ``sign``).
3.  **Methoden/Wege**: Konto & Positionen sind signierte **GET**-Aufrufe auf
    ``/api/v1/futures/account`` bzw. ``/api/v1/futures/position/get_pending_positions``;
    Orders laufen über ``/api/v1/futures/trade/{place_order,cancel_orders}`` und
    ``/api/v1/futures/trade/get_order_detail``.
4.  **Symbole**: Bitunix kennt nur ``BTCUSDT`` (kein ``BTC/USDT``).
5.  **Kline**: Antwort ist eine Liste von *Objekten* (open/high/low/close/time/
    baseVol), nicht von Arrays; ``limit`` ist max. 200.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from .symbols import normalize_symbol, to_exchange_symbol
from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.net import get_breaker, classify_error, retry_call, throttle

log = get_logger("bitunix")

_KLINE_MAX_LIMIT = 200
_TIMEFRAMES = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
               "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h",
               "1d": "1d", "1w": "1w"}

# öffentliche (nicht signierte) Marktdaten-Pfade
PUB_KLINE = "/api/v1/futures/market/kline"
PUB_TICKERS = "/api/v1/futures/market/tickers"
PUB_PAIRS = "/api/v1/futures/market/trading_pairs"
PUB_FUNDING = "/api/v1/futures/market/funding_rate"
PRV_ACCOUNT = "/api/v1/futures/account"
PRV_POSITIONS = "/api/v1/futures/position/get_pending_positions"
PRV_PLACE = "/api/v1/futures/trade/place_order"
PRV_CANCEL = "/api/v1/futures/trade/cancel_orders"
PRV_ORDER = "/api/v1/futures/trade/get_order_detail"
PRV_LEVERAGE = "/api/v1/futures/account/change_leverage"


class BitunixAPIError(RuntimeError):
    def __init__(self, code: Any, msg: str, path: str = ""):
        super().__init__(f"code={code} {msg} [{path}]".strip())
        self.code = code
        self.msg = msg
        self.path = path


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class BitunixExchange(Exchange):
    name = "bitunix"

    def __init__(self) -> None:
        self.api_key = cfg.BITUNIX_API_KEY
        self.secret = cfg.BITUNIX_SECRET_KEY
        self.base = (cfg.BITUNIX_BASE_URL or "https://fapi.bitunix.com").rstrip("/")
        self.margin_coin = cfg.BITUNIX_MARGIN_COIN
        self.session = requests.Session()
        self.breaker = get_breaker(self.name)
        self.read_only = bool(cfg.BITUNIX_TESTNET and not cfg.BITUNIX_ALLOW_LIVE_ORDERS)
        self._markets_cache: dict[str, dict] = {}
        log.info(
            "Bitunix initialized. base=%s key_set=%s mode=%s",
            self.base, bool(self.api_key),
            "read-only (kein API-Testnet – Orders blockiert)" if self.read_only else "TRADING",
        )
        if cfg.BITUNIX_TESTNET and not cfg.BITUNIX_ALLOW_LIVE_ORDERS:
            log.warning(
                "BITUNIX_TESTNET=true, aber Bitunix hat kein öffentliches REST-Testnet. "
                "Adapter läuft read-only gegen %s. Echte Orders erst mit "
                "BITUNIX_ALLOW_LIVE_ORDERS=true (und DRY_RUN=false).", self.base)

    # ---- Signatur & HTTP -------------------------------------------------
    def _nonce(self) -> str:
        return os.urandom(16).hex()  # 32 Zeichen, wie in der Doku gefordert

    @staticmethod
    def _query_string(params: dict) -> str:
        """queryParams = aufsteigend nach Key sortiert, 'key+value' ohne Trennzeichen."""
        return "".join(f"{k}{params[k]}" for k in sorted(params)) if params else ""

    def _sign(self, params: dict, body_str: str, nonce: str, timestamp: str) -> str:
        digest = _sha256_hex(nonce + timestamp + self.api_key + self._query_string(params) + body_str)
        return _sha256_hex(digest + self.secret)

    def _headers(self, params: dict, body_str: str) -> dict:
        nonce = self._nonce()
        ts = str(int(time.time() * 1000))
        return {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": ts,
            "sign": self._sign(params, body_str, nonce, ts),
            "Content-Type": "application/json",
            "language": "en-US",
        }

    def _check(self, payload: dict, path: str) -> Any:
        if not isinstance(payload, dict):
            raise BitunixAPIError("bad_response", f"unexpected payload {str(payload)[:120]}", path)
        code = payload.get("code")
        if code not in (0, "0", 200, None):
            raise BitunixAPIError(code, str(payload.get("msg") or payload)[:200], path)
        return payload.get("data", payload.get("result"))

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 body: Optional[dict] = None, signed: bool = True) -> Any:
        allowed, why = self.breaker.allow()
        if not allowed:
            log.debug("Bitunix %s skipped: %s", path, why)
            return None
        params = {k: v for k, v in (params or {}).items() if v is not None}
        # Body byte-identisch serialisieren – die Signatur enthält genau diesen String.
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        url = self.base + path

        def do() -> Any:
            throttle(f"bitunix:{path}", 0.12)  # Rate-Limit: 10 req/sec
            headers = self._headers(params, body_str) if signed else {"Content-Type": "application/json"}
            resp = self.session.request(
                method.upper(), url, params=params or None,
                data=body_str if method.upper() != "GET" else None,
                headers=headers, timeout=cfg.HTTP_TIMEOUT_S)
            if resp.status_code in (401, 403):
                raise BitunixAPIError(resp.status_code,
                                      f"auth/check rejected – API-Key prüfen (Bitunix hat kein Testnet; "
                                      f"Demo-Keys funktionieren nur im Web-UI): {resp.text[:160]}", path)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise BitunixAPIError(resp.status_code, f"HTTP {resp.status_code} {resp.text[:120]}", path)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError as e:
                raise BitunixAPIError("bad_json", resp.text[:160], path) from e
            return self._check(payload, path)

        try:
            out = retry_call(do, attempts=cfg.HTTP_MAX_RETRIES,
                             on_retry=lambda i, e: log.warning("Bitunix %s retry %d: %s", path, i, e))
            self.breaker.record_success()
            return out
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            msg = self.breaker.record_failure(e)
            log.error("Bitunix %s failed (%s): %s", path, kind, msg)
            return None

    # ---- Exchange-Interface ----------------------------------------------
    def get_account(self) -> dict:
        try:
            data = self._request("GET", PRV_ACCOUNT, params={"marginCoin": self.margin_coin})
            if data is None:
                return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": self.margin_coin,
                        "error": self.breaker.last_error or "bitunix unreachable"}
            # Antwort ist laut Doku eine Liste von Coins
            entry = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            f = lambda k: float(entry.get(k) or 0)  # noqa: E731
            available, frozen, margin = f("available"), f("frozen"), f("margin")
            unreal = f("crossUnrealizedPNL") + f("isolationUnrealizedPNL")
            return {
                "equity": available + frozen + margin + unreal,
                "cash": available,
                "buying_power": available,
                "currency": entry.get("marginCoin", self.margin_coin),
                "unrealized_pnl": unreal,
                "read_only": self.read_only,
            }
        except Exception as e:  # noqa: BLE001
            log.error("Bitunix get_account failed: %s", e)
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": "USDT", "error": str(e)[:200]}

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        try:
            data = self._request("GET", PRV_POSITIONS) or []
            for p in data if isinstance(data, list) else [data]:
                if not isinstance(p, dict):
                    continue
                qty = float(p.get("qty") or p.get("positionAmt") or 0)
                if qty == 0:
                    continue
                side = str(p.get("side") or "LONG").upper()
                entry_price = p.get("avgOpenPrice") or p.get("entryValue") or 0
                out.append(Position(
                    symbol=normalize_symbol(str(p.get("symbol", "")), self.name),
                    qty=qty if side != "SHORT" else -qty,
                    avg_entry=float(entry_price or 0),
                    market="crypto_perp",
                    broker=self.name,
                    unrealized_pnl=float(p.get("unrealizedPNL") or 0),
                    leverage=int(float(p.get("leverage") or 1)),
                ))
        except Exception as e:  # noqa: BLE001
            log.error("Bitunix get_positions failed: %s", e)
        return out

    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        tf = _TIMEFRAMES.get((timeframe or "1h").lower(), "1h")
        limit = max(5, min(int(limit or 100), _KLINE_MAX_LIMIT))
        raw = self._request("GET", PUB_KLINE, signed=False,
                            params={"symbol": to_exchange_symbol(symbol, self.name),
                                    "interval": tf, "limit": limit, "type": "LAST_PRICE"})
        if not raw:
            return []
        rows = raw if isinstance(raw, list) else raw.get("data", [])
        bars: list[Bar] = []
        for k in rows or []:
            try:
                if isinstance(k, dict):
                    ts = k.get("time") or k.get("timestamp") or k.get("openTime")
                    o, h, l, c = k.get("open"), k.get("high"), k.get("low"), k.get("close")
                    v = k.get("baseVol") or k.get("volume") or k.get("quoteVol") or 0
                else:  # Array-Form [ts, o, h, l, c, v]
                    ts, o, h, l, c, v = list(k)[:6]
                bars.append(Bar(
                    symbol=normalize_symbol(symbol, self.name),
                    timestamp=datetime.fromtimestamp(int(float(ts)) / 1000, tz=timezone.utc),
                    open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0),
                ))
            except (TypeError, ValueError, IndexError) as e:
                log.debug("Bitunix kline row skipped (%s): %s", e, k)
                continue
        return bars

    def place_order(self, order: Order) -> Order:
        if cfg.DRY_RUN:
            log.info("[DRY_RUN] Bitunix: %s %s %.6f @ref=%s", order.side.value, order.symbol,
                     order.qty, order.avg_fill_price or order.limit_price or "mkt")
            order.status = OrderStatus.FILLED
            order.filled_qty = order.qty
            order.avg_fill_price = order.avg_fill_price or 0.0
            order.broker_order_id = f"dry-bu-{int(time.time() * 1000)}"
            return order
        if self.read_only:
            order.status = OrderStatus.REJECTED
            order.error = ("Bitunix läuft im Modus 'read-only' (BITUNIX_TESTNET=true, kein API-Testnet). "
                           "Echte Orders erst mit BITUNIX_ALLOW_LIVE_ORDERS=true.")
            log.error("Bitunix order blocked: %s %s → %s", order.side.value, order.symbol, order.error)
            return order
        sym = to_exchange_symbol(order.symbol, self.name)
        body: dict[str, Any] = {
            "symbol": sym,
            "side": "BUY" if order.side == Side.BUY else "SELL",
            "orderType": "MARKET" if order.type == OrderType.MARKET else "LIMIT",
            "qty": f"{float(order.qty):.10f}".rstrip("0").rstrip("."),
            "tradeSide": "OPEN",
        }
        if order.type != OrderType.MARKET and order.limit_price:
            body["price"] = f"{float(order.limit_price):.10f}".rstrip("0").rstrip(".")
            body["effect"] = "GTC"
        if order.stop_loss:
            body.update({"slPrice": f"{float(order.stop_loss):.10f}".rstrip("0").rstrip("."),
                         "slStopType": "MARK_PRICE", "slOrderType": "MARKET"})
        if order.take_profit:
            body.update({"tpPrice": f"{float(order.take_profit):.10f}".rstrip("0").rstrip("."),
                         "tpStopType": "MARK_PRICE", "tpOrderType": "MARKET"})
        if order.client_order_id:
            body["clientId"] = order.client_order_id[:64]
        data = self._request("POST", PRV_PLACE, body=body)
        if not data:
            order.status = OrderStatus.REJECTED
            order.error = self.breaker.last_error or "no response"
            return order
        order.broker_order_id = str(data.get("orderId") or data.get("clientId") or "") or None
        order.status = OrderStatus.NEW
        log.info("Bitunix order %s submitted (%s %s %s)", order.broker_order_id, body["side"], sym, body["qty"])
        return order

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        if self.read_only:
            return False
        data = self._request("POST", PRV_LEVERAGE, body={
            "symbol": to_exchange_symbol(symbol, self.name),
            "leverage": int(leverage), "marginCoin": self.margin_coin})
        return bool(data)

    def cancel_order(self, broker_order_id: str, symbol: str = "") -> bool:
        body = {"orderList": [{"orderId": str(broker_order_id)}]}
        if symbol:
            body["symbol"] = to_exchange_symbol(symbol, self.name)
        data = self._request("POST", PRV_CANCEL, body=body)
        return bool(data)

    def get_order(self, broker_order_id: str) -> Order:
        data = self._request("GET", PRV_ORDER, params={"orderId": str(broker_order_id)})
        if not data or not isinstance(data, dict):
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0,
                         status=OrderStatus.UNKNOWN, error=self.breaker.last_error or "order not found")
        status_map = {
            "INIT": OrderStatus.NEW, "NEW": OrderStatus.NEW, "PART_FILLED": OrderStatus.PARTIAL,
            "PARTIALLY_FILLED": OrderStatus.PARTIAL, "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED,
        }
        qty = float(data.get("qty") or 0)
        filled = float(data.get("tradeQty") or data.get("executedQty") or 0)
        return Order(
            broker=self.name,
            symbol=normalize_symbol(str(data.get("symbol", "")), self.name),
            side=Side.BUY if str(data.get("side", "")).upper() == "BUY" else Side.SELL,
            qty=qty,
            filled_qty=filled,
            # Bitunix liefert in get_order_detail keinen Durchschnittspreis –
            # ohne Fill-Preis bleibt der Order-Preis Referenz (0 => später aus Fillslog).
            avg_fill_price=float(data.get("avgPrice") or data.get("price") or 0),
            broker_order_id=str(data.get("orderId") or broker_order_id),
            status=status_map.get(str(data.get("status", "")).upper(), OrderStatus.UNKNOWN),
        )

    def is_market_open(self, symbol: str) -> bool:
        # Perps handeln 24/7; bei gestörter Verbindung kennen wir den Status nicht.
        return not self.breaker.open

    # ---- Helper ----------------------------------------------------------
    def trading_pairs(self, symbols: Optional[list[str]] = None) -> dict[str, dict]:
        """Instrument-Metadaten (minTradeVolume, Prezisision) – gecacht."""
        if self._markets_cache and not symbols:
            return self._markets_cache
        key = ",".join(sorted(symbols)) if symbols else ""
        data = self._request("GET", PUB_PAIRS, signed=False,
                             params={"symbols": key} if key else None) or []
        out: dict[str, dict] = {}
        for m in data if isinstance(data, list) else []:
            if isinstance(m, dict) and m.get("symbol"):
                out[str(m["symbol"])] = m
        if not key:
            self._markets_cache = out
        return out

    def is_tradable(self, symbol: str) -> bool:
        s = to_exchange_symbol(symbol, self.name)
        pairs = self.trading_pairs()
        if not pairs:  # Endpoint nicht erreichbar -> nicht als "unanbietbar" werten
            return True
        info = pairs.get(s)
        return bool(info) and bool(info.get("isApiSupported", True)) and str(info.get("symbolStatus", "OPEN")) == "OPEN"

    def status(self) -> dict:
        return {"base": self.base, "read_only": self.read_only, "breaker": self.breaker.note(),
                "failures": self.breaker.failures, "last_error": self.breaker.last_error}
