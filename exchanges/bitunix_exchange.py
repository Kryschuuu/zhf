"""Minimal Bitunix adapter (nicht in CCXT vorhanden).

Dokumentation: https://bitunix.com/api-docs
Wir implementieren nur was der Execution Agent braucht: Account, Positions,
Bars, Place Order, Cancel Order.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import requests

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("bitunix")


class BitunixExchange(Exchange):
    name = "bitunix"

    def __init__(self) -> None:
        self.api_key = cfg.BITUNIX_API_KEY
        self.secret = cfg.BITUNIX_SECRET_KEY
        self.base = "https://fapi.bitunix.com" if not cfg.BITUNIX_TESTNET else "https://fapi-sim.bitunix.com"
        self.session = requests.Session()
        self.recv_window = 5000
        log.info("Bitunix initialized. testnet=%s key_set=%s", cfg.BITUNIX_TESTNET, bool(self.api_key))

    # ---- HTTP helpers ----
    def _sign(self, params: dict) -> dict:
        params["apiKey"] = self.api_key
        params["timestamp"] = str(int(time.time() * 1000))
        params["nonce"] = str(int(time.time() * 1000))
        qs = urllib.parse.urlencode(sorted(params.items()))
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["sign"] = sig
        return params

    def _get(self, path: str, params: Optional[dict] = None, signed: bool = False) -> dict:
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        r = self.session.get(self.base + path, params=params, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("code", 0) not in (0, "0", 200, None):
            log.error("Bitunix GET %s error: %s", path, d)
        return d

    def _post(self, path: str, data: Optional[dict] = None) -> dict:
        data = self._sign(dict(data or {}))
        r = self.session.post(self.base + path, json=data, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("code", 0) not in (0, "0", 200, None):
            log.error("Bitunix POST %s error: %s", path, d)
        return d

    # ---- Exchange interface ----
    def get_account(self) -> dict:
        try:
            d = self._post("/api/v1/futures/account", {})
            data = d.get("data", {})
            return {
                "equity": float(data.get("equity") or data.get("totalWalletBalance") or 0),
                "cash": float(data.get("availableBalance") or 0),
                "buying_power": float(data.get("availableBalance") or 0),
                "currency": "USDT",
            }
        except Exception as e:
            log.error("Bitunix get_account failed: %s", e)
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": "USDT", "error": str(e)}

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        try:
            d = self._post("/api/v1/futures/positions", {})
            for p in d.get("data", []) or []:
                qty = float(p.get("positionAmt") or p.get("contracts") or 0)
                if qty == 0:
                    continue
                side = p.get("positionSide", "BOTH")
                qty_signed = qty if side in ("LONG", "BOTH") and float(p.get("entryPrice", 0)) > 0 else -qty
                out.append(Position(
                    symbol=p.get("symbol", ""),
                    qty=qty_signed,
                    avg_entry=float(p.get("entryPrice") or 0),
                    market="crypto_perp",
                    broker=self.name,
                    unrealized_pnl=float(p.get("unrealizedProfit") or 0),
                    leverage=int(p.get("leverage") or 1),
                ))
        except Exception as e:
            log.error("Bitunix get_positions failed: %s", e)
        return out

    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}
        tf = tf_map.get(timeframe, "1h")
        try:
            d = self._get("/api/v1/futures/kline", {
                "symbol": symbol, "interval": tf, "limit": limit,
            })
            bars = []
            for k in d.get("data", []) or []:
                # k = [ts, o, h, l, c, v]
                if not isinstance(k, (list, tuple)) or len(k) < 6:
                    continue
                ts, o, h, l, c, v = k[:6]
                bars.append(Bar(
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc),
                    open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v),
                ))
            return bars
        except Exception as e:
            log.error("Bitunix get_bars failed: %s", e)
            return []

    def place_order(self, order: Order) -> Order:
        if cfg.DRY_RUN:
            log.info("[DRY_RUN] Bitunix: %s %s %.4f", order.side.value, order.symbol, order.qty)
            order.status = OrderStatus.FILLED
            order.filled_qty = order.qty
            order.broker_order_id = f"dry-bx-{int(time.time())}"
            return order
        side = "BUY" if order.side == Side.BUY else "SELL"
        otype = "MARKET" if order.type == OrderType.MARKET else "LIMIT"
        body: dict = {
            "symbol": order.symbol,
            "side": side,
            "type": otype,
            "quantity": str(order.qty),
        }
        if order.type != OrderType.MARKET:
            body["price"] = str(order.limit_price)
            body["timeInForce"] = "GTC"
        try:
            d = self._post("/api/v1/futures/order", body)
            order.broker_order_id = str((d.get("data") or {}).get("orderId", ""))
            order.status = OrderStatus.NEW
        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.error = str(e)
            log.error("Bitunix place_order failed: %s", e)
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self._post("/api/v1/futures/order/cancel", {"orderId": broker_order_id})
            return True
        except Exception as e:
            log.error("Bitunix cancel_order failed: %s", e)
            return False

    def get_order(self, broker_order_id: str) -> Order:
        try:
            d = self._post("/api/v1/futures/order/query", {"orderId": broker_order_id})
            o = d.get("data", {})
            status_map = {
                "NEW": OrderStatus.NEW, "PARTIALLY_FILLED": OrderStatus.PARTIAL,
                "FILLED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED,
                "REJECTED": OrderStatus.REJECTED,
            }
            return Order(
                broker=self.name,
                symbol=o.get("symbol", ""),
                side=Side.BUY if o.get("side") == "BUY" else Side.SELL,
                qty=float(o.get("origQty") or 0),
                filled_qty=float(o.get("executedQty") or 0),
                avg_fill_price=float(o.get("avgPrice") or 0),
                broker_order_id=str(o.get("orderId")),
                status=status_map.get(str(o.get("status")), OrderStatus.UNKNOWN),
            )
        except Exception as e:
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0, status=OrderStatus.UNKNOWN, error=str(e))

    def is_market_open(self, symbol: str) -> bool:
        return True
