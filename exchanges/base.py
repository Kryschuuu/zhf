"""Unified exchange interface.

Alle Broker implementieren dieselben Methoden, damit Execution Agent nicht
wissen muss, ob er mit Alpaca, BingX oder Bitunix spricht.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    PARTIAL = "PARTIALLY_FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Position:
    symbol: str
    qty: float          # pos. = long, neg. = short
    avg_entry: float
    market: str         # stocks | crypto | forex
    broker: str
    unrealized_pnl: float = 0.0
    leverage: int = 1


@dataclass
class Order:
    broker: str
    symbol: str
    side: Side
    qty: float
    type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    leverage: int = 1
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    error: Optional[str] = None


@dataclass
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Exchange(ABC):
    name: str = "abstract"

    @abstractmethod
    def get_account(self) -> dict:
        """Return dict with equity, cash, buying_power."""

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        """timeframe: '1m','5m','15m','1h','1d'"""

    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool: ...

    @abstractmethod
    def get_order(self, broker_order_id: str) -> Order: ...

    @abstractmethod
    def is_market_open(self, symbol: str) -> bool: ...
