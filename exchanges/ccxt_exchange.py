"""CCXT-basierte Adapter für BingX & Bitunix (Spot + USDT-M Perps).

CCXT ist der Standard für Crypto-Exchanges und erspart uns die eigene
HMAC-Signierung. Beide Exchanges sind in CCXT integriert.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import ccxt

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("ccxt")

_CCXT_TIMEFRAME = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}


class CCXTExchange(Exchange):
    def __init__(self, exchange_id: str, api_key: str, secret: str, testnet: bool):
        self.exchange_id = exchange_id
        cls = getattr(ccxt, exchange_id)
        params: dict = {"apiKey": api_key, "secret": secret, "enableRateLimit": True}
        options: dict = {}
        if exchange_id == "bingx":
            # BingX bietet keine öffentliche Sandbox für Perps – TESTNET=true schaltet
            # auf den Demo-Endpunkt via ccxt, falls von der aktuellen ccxt-Version
            # unterstützt; sonst auf ein dediziertes Sub-Konto mit Spielgeld setzen.
            options["defaultType"] = "swap"
            params["sandboxMode"] = bool(testnet)
        else:
            options["defaultType"] = "swap"
            params["sandboxMode"] = bool(testnet)
        if options:
            params["options"] = options
        self.ex = cls(params)
        if not api_key:
            log.warning("%s started WITHOUT API keys – only public data will work.", exchange_id)
        try:
            self.ex.load_markets()
            log.info("%s: %d markets loaded (testnet=%s)", exchange_id, len(self.ex.markets), testnet)
        except Exception as e:
            log.warning("%s load_markets failed: %s", exchange_id, e)

    @property
    def name(self) -> str:
        return self.exchange_id

    def get_account(self) -> dict:
        try:
            bal = self.ex.fetch_balance()
            usdt = float(bal.get("USDT", {}).get("total", 0) or bal.get("total", {}).get("USDT", 0))
            free = float(bal.get("USDT", {}).get("free", 0) or bal.get("free", {}).get("USDT", 0))
            return {"equity": usdt, "cash": free, "buying_power": free, "currency": "USDT"}
        except Exception as e:
            log.error("%s get_account failed: %s", self.name, e)
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": "USDT", "error": str(e)}

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        try:
            positions = self.ex.fetch_positions() if hasattr(self.ex, "fetch_positions") else []
            for p in positions:
                qty = float(p.get("contracts") or p.get("positionAmt") or 0)
                if qty == 0:
                    continue
                out.append(Position(
                    symbol=p.get("symbol", ""),
                    qty=qty if p.get("side", "long") == "long" else -qty,
                    avg_entry=float(p.get("entryPrice") or 0),
                    market="crypto_perp",
                    broker=self.name,
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                    leverage=int(p.get("leverage") or 1),
                ))
        except Exception as e:
            log.error("%s get_positions failed: %s", self.name, e)
        return out

    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        try:
            tf = _CCXT_TIMEFRAME.get(timeframe, "1h")
            ohlcv = self.ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            bars = []
            for row in ohlcv[-limit:]:
                ts, o, h, l, c, v = row
                bars.append(Bar(
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                    open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v),
                ))
            return bars
        except Exception as e:
            log.error("%s get_bars(%s) failed: %s", self.name, symbol, e)
            return []

    def place_order(self, order: Order) -> Order:
        if cfg.DRY_RUN:
            log.info("[DRY_RUN] %s: %s %s %.4f %s", self.name, order.side.value, order.symbol, order.qty, order.type.value)
            order.status = OrderStatus.FILLED
            order.filled_qty = order.qty
            order.avg_fill_price = 0.0
            order.broker_order_id = f"dry-{order.client_order_id or int(time.time())}"
            return order
        side = "buy" if order.side == Side.BUY else "sell"
        otype = "market" if order.type == OrderType.MARKET else "limit"
        params: dict = {}
        if order.stop_loss:
            params["stopLoss"] = {"triggerPrice": order.stop_loss}
        if order.take_profit:
            params["takeProfit"] = {"triggerPrice": order.take_profit}
        try:
            res = self.ex.create_order(order.symbol, otype, side, order.qty, order.limit_price, params=params or None)
            order.broker_order_id = str(res.get("id", ""))
            order.status = OrderStatus.NEW
            if res.get("status") == "closed":
                order.status = OrderStatus.FILLED
                order.filled_qty = float(res.get("filled", order.qty))
                order.avg_fill_price = float(res.get("average") or res.get("price") or 0)
        except Exception as e:
            log.error("%s place_order failed: %s", self.name, e)
            order.status = OrderStatus.REJECTED
            order.error = str(e)
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self.ex.cancel_order(broker_order_id)
            return True
        except Exception as e:
            log.error("%s cancel_order(%s) failed: %s", self.name, broker_order_id, e)
            return False

    def get_order(self, broker_order_id: str) -> Order:
        try:
            o = self.ex.fetch_order(broker_order_id)
            status_map = {
                "open": OrderStatus.NEW, "closed": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELED, "rejected": OrderStatus.REJECTED,
            }
            return Order(
                broker=self.name,
                symbol=o.get("symbol", ""),
                side=Side.BUY if o.get("side") == "buy" else Side.SELL,
                qty=float(o.get("amount") or 0),
                filled_qty=float(o.get("filled") or 0),
                avg_fill_price=float(o.get("average") or o.get("price") or 0),
                broker_order_id=str(o.get("id")),
                status=status_map.get(str(o.get("status")), OrderStatus.UNKNOWN),
            )
        except Exception as e:
            log.error("%s get_order failed: %s", self.name, e)
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0, status=OrderStatus.UNKNOWN, error=str(e))

    def is_market_open(self, symbol: str) -> bool:
        return True  # Crypto 24/7


def make_bingx() -> CCXTExchange:
    return CCXTExchange("bingx", cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY, cfg.BINGX_TESTNET)


def make_bitunix() -> CCXTExchange:
    return CCXTExchange("bitunix", cfg.BITUNIX_API_KEY, cfg.BITUNIX_SECRET_KEY, cfg.BITUNIX_TESTNET)
