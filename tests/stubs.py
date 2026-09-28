"""Gemeinsame Stubs für die Agent-Tests (kein Netzwerk, keine Broker)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from exchanges.base import Bar, Order, OrderStatus, Position, Side


def make_bars(n: int = 120, start: float = 100.0, trend: float = 0.0,
               noise: float = 0.0, symbol: str = "AAA", timeframe: str = "1h",
               drop_last: bool = False) -> list[Bar]:
    """Deterministische Kerzen; `trend` < 0 erzeugt Abwärtsbewegung (RSI tief)."""
    step = timedelta(hours=1) if timeframe == "1h" else timedelta(days=1)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - (
        step if drop_last else timedelta(0))
    bars: list[Bar] = []
    price = start
    for i in range(n):
        price = max(0.5, price * (1 + trend + noise * math.sin(i / 3)))
        bars.append(Bar(symbol=symbol, timestamp=end - step * (n - 1 - i),
                        open=price, high=price * 1.01, low=price * 0.99, close=price,
                        volume=1_000_000 + 1000 * i))
    return bars


class StubExchange:
    """Minimal-Exchange: liefert vorgegebene Bars/Positionen und zählt Aufrufe."""

    name = "stub"

    def __init__(self, bars: list[Bar] | None = None, *, equity: float = 10_000.0,
                 cash: float = 8_000.0, positions: list[Position] | None = None,
                 tradable: set[str] | None = None, bars_by_symbol: dict[str, list[Bar]] | None = None,
                 raise_on_bars: Exception | None = None, read_only: bool = False):
        self.bars = bars if bars is not None else make_bars()
        self.bars_by_symbol = bars_by_symbol or {}
        self.equity, self.cash = equity, cash
        self.positions = positions or []
        self.tradable = tradable            # None = alles erlaubt
        self.raise_on_bars = raise_on_bars
        self.read_only = read_only
        self.bar_calls: list[tuple[str, str, int]] = []
        self.orders: list[Order] = []
        self.market_open = True

    # --- Daten ---
    def get_bars(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[Bar]:
        self.bar_calls.append((symbol, timeframe, limit))
        if self.raise_on_bars:
            raise self.raise_on_bars
        return list(self.bars_by_symbol.get(symbol, self.bars))[-limit:]

    def get_account(self) -> dict:
        return {"equity": self.equity, "cash": self.cash, "buying_power": self.cash,
                "currency": "USD"}

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def is_market_open(self, symbol: str) -> bool:
        return self.market_open

    def is_tradable(self, symbol: str) -> bool:
        if self.tradable is None:
            return not any(x in symbol.upper() for x in ("SYNTH", "TEST_"))
        return symbol in self.tradable

    def status(self) -> dict:
        return {"stub": True, "read_only": self.read_only}

    # --- Orders ---
    def place_order(self, order: Order) -> Order:
        self.orders.append(order)
        ref = order.entry_reference_price or (self.bars[-1].close if self.bars else 100.0)
        order.broker_order_id = f"stub-{len(self.orders)}"
        order.status = OrderStatus.FILLED
        order.filled_qty = order.qty
        order.avg_fill_price = ref * 1.0003
        return order

    def get_order(self, broker_order_id: str) -> Order:
        for o in self.orders:
            if o.broker_order_id == broker_order_id:
                return o
        return Order(broker=self.name, symbol="", side=Side.BUY, qty=0,
                     status=OrderStatus.UNKNOWN, error="unknown")

    def cancel_order(self, broker_order_id: str) -> bool:
        return True

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        return True

    def data_feed_status(self) -> dict:
        return {}
