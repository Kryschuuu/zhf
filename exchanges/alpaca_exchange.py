"""Alpaca adapter – stocks, ETFs, crypto via alpaca-py."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("alpaca")

_TIMEFRAME_MAP = {
    "1m": TimeFrame(1, TimeFrameUnit.Minute),
    "5m": TimeFrame(5, TimeFrameUnit.Minute),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "1h": TimeFrame(1, TimeFrameUnit.Hour),
    "1d": TimeFrame(1, TimeFrameUnit.Day),
}


class AlpacaExchange(Exchange):
    name = "alpaca"

    def __init__(self) -> None:
        self.client = TradingClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY, paper=cfg.ALPACA_PAPER)
        self.stock_data = StockHistoricalDataClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY)
        self.crypto_data = CryptoHistoricalDataClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY)
        acc = self.client.get_account()
        log.info("Alpaca connected. Paper=%s Equity=%s Cash=%s", cfg.ALPACA_PAPER, acc.equity, acc.cash)

    def get_account(self) -> dict:
        acc = self.client.get_account()
        return {
            "equity": float(acc.equity),
            "cash": float(acc.cash),
            "buying_power": float(acc.buying_power),
            "currency": acc.currency,
        }

    def get_positions(self) -> list[Position]:
        out = []
        for p in self.client.get_all_positions():
            qty = float(p.qty)
            out.append(Position(
                symbol=p.symbol,
                qty=qty,
                avg_entry=float(p.avg_entry_price),
                market="crypto" if "/" in p.symbol else "stocks",
                broker=self.name,
                unrealized_pnl=float(p.unrealized_pl),
            ))
        return out

    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        tf = _TIMEFRAME_MAP.get(timeframe, TimeFrame(1, TimeFrameUnit.Hour))
        end = datetime.utcnow()
        # Für Aktien nur Handelstage; Crypto 24/7. Mit Back-Stop für Wochenenden.
        start = end - timedelta(days=max(30, limit * 2))
        is_crypto = "/" in symbol or symbol.upper() in ("BTC", "ETH", "SOL", "DOGE")
        try:
            if is_crypto:
                req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=tf, start=start, end=end, limit=limit)
                resp = self.crypto_data.get_crypto_bars(req)
            else:
                req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=tf, start=start, end=end, limit=limit)
                resp = self.stock_data.get_stock_bars(req)
            df = resp.df
            if hasattr(df, "index"):
                df = df.reset_index()
            bars: list[Bar] = []
            for _, row in df.tail(limit).iterrows():
                ts = row.get("timestamp", row.name if hasattr(row, "name") else datetime.utcnow())
                bars.append(Bar(
                    symbol=symbol,
                    timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                ))
            return bars
        except Exception as e:
            log.error("Alpaca get_bars(%s) failed: %s", symbol, e)
            return []

    def place_order(self, order: Order) -> Order:
        if cfg.DRY_RUN:
            log.info("[DRY_RUN] Would place order %s %s %.4f %s on Alpaca", order.side.value, order.symbol, order.qty, order.type.value)
            order.status = OrderStatus.FILLED
            order.filled_qty = order.qty
            order.avg_fill_price = 0.0
            order.broker_order_id = f"dry-{order.client_order_id or '0'}"
            return order
        side = AlpacaOrderSide.BUY if order.side == Side.BUY else AlpacaOrderSide.SELL
        try:
            if order.type == OrderType.MARKET:
                req = MarketOrderRequest(
                    symbol=order.symbol,
                    qty=order.qty if order.qty >= 1 else None,
                    notional=None if order.qty >= 1 else round(order.qty, 2),
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=order.client_order_id,
                )
            else:
                req = LimitOrderRequest(
                    symbol=order.symbol,
                    limit_price=order.limit_price,
                    qty=order.qty,
                    side=side,
                    time_in_force=TimeInForce.GTC,
                    client_order_id=order.client_order_id,
                )
            res = self.client.submit_order(req)
            order.broker_order_id = str(res.id)
            order.status = OrderStatus.NEW
        except Exception as e:
            log.error("Alpaca place_order failed: %s", e)
            order.status = OrderStatus.REJECTED
            order.error = str(e)
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(broker_order_id)
            return True
        except Exception as e:
            log.error("Cancel order %s failed: %s", broker_order_id, e)
            return False

    def get_order(self, broker_order_id: str) -> Order:
        try:
            o = self.client.get_order_by_id(broker_order_id)
            status_map = {
                "new": OrderStatus.NEW, "partially_filled": OrderStatus.PARTIAL,
                "filled": OrderStatus.FILLED, "canceled": OrderStatus.CANCELED,
                "rejected": OrderStatus.REJECTED,
            }
            return Order(
                broker=self.name,
                symbol=o.symbol,
                side=Side.BUY if o.side == "buy" else Side.SELL,
                qty=float(o.qty),
                filled_qty=float(o.filled_qty or 0),
                avg_fill_price=float(o.filled_avg_price or 0),
                broker_order_id=str(o.id),
                status=status_map.get(str(o.status), OrderStatus.UNKNOWN),
            )
        except Exception as e:
            log.error("get_order %s failed: %s", broker_order_id, e)
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0, status=OrderStatus.UNKNOWN, error=str(e))

    def is_market_open(self, symbol: str) -> bool:
        if "/" in symbol or symbol.upper() in ("BTC", "ETH", "SOL"):
            return True  # Crypto 24/7
        try:
            clock = self.client.get_clock()
            return bool(clock.is_open)
        except Exception:
            return False
