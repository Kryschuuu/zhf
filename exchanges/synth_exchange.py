"""Synth-Broker: deterministischer Fake-Exchange für End-to-End-Tests.

Aktiv mit ``ZHF_SYNTH=1`` (oder ``SYNTH_WATCHLIST=1``). Der Broker liefert
 reproduzierbare OHLCV-Kerzen aus einem Seed, ein fixes Demo-Portfolio und
faked Fills zum Referenzpreis plus Slippage. Damit läuft die komplette Pipeline
(Research → Backtest → Risk → Execution → Cost) ohne API-Keys, ohne Netz und
ohne Risiko – und vor allem **ohne** dass synthetische Symbole wie ``SYNTH_A``
bei Alpaca landen (die Cause der ``invalid symbol: SYNTH_A``-Fehlermeldungen).
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from .symbols import dedupe_bars, is_valid_symbol_for_broker, normalize_symbol
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("synth")

_SEED = 42


class SynthExchange(Exchange):
    name = "synth"

    def __init__(self, equity: float = 10_000.0, bars: int = 300,
                 seed: Optional[int] = None, slippage_bps: float = 3.0):
        self.equity = float(equity)
        self.cash = float(equity) * 0.85
        self.n_bars = int(bars)
        self.seed = int(cfg.SYNTH_SEED if seed is None else seed)
        self.slippage_bps = float(slippage_bps)
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self._bar_cache: dict[str, list[Bar]] = {}

    # ------------------------------------------------------------- market data
    def _price_series(self, symbol: str, n: int) -> list[float]:
        rnd = random.Random(f"{self.seed}:{symbol}")
        base = 100.0 + (abs(hash(symbol)) % 900)
        prices = [base]
        for i in range(1, n):
            drift = 0.0004 * math.sin(i / 24)
            prices.append(max(0.01, prices[-1] * (1 + rnd.gauss(drift, 0.012))))
        return prices

    def get_bars(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[Bar]:
        key = f"{symbol}:{timeframe}:{limit}"
        if key in self._bar_cache:
            return self._bar_cache[key]
        n = min(max(30, int(limit)), self.n_bars)
        prices = self._price_series(symbol, n)
        rnd = random.Random(f"{self.seed}:bars:{symbol}")
        step = timedelta(hours=1) if (timeframe or "1h") == "1h" else timedelta(minutes=5) \
            if (timeframe or "1h") in ("1m", "5m", "15m", "30m") else timedelta(days=1)
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        bars: list[Bar] = []
        for i, c in enumerate(prices):
            ts = end - step * (n - i)
            o = c * (1 + rnd.gauss(0, 0.004))
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.006)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.006)))
            bars.append(Bar(symbol=normalize_symbol(symbol, "synth"), timestamp=ts,
                            open=round(o, 6), high=round(h, 6), low=round(l, 6),
                            close=round(c, 6), volume=float(rnd.randint(1_000_000, 5_000_000))))
        out = dedupe_bars(bars)
        self._bar_cache[key] = out
        return out

    # ------------------------------------------------------------------ account
    def get_account(self) -> dict:
        return {"equity": self.equity, "cash": self.cash, "buying_power": self.cash,
                "currency": "USD", "mode": "synth"}

    def get_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if abs(p.qty) > 1e-12]

    def is_market_open(self, symbol: str) -> bool:
        return True

    def is_tradable(self, symbol: str) -> bool:
        """Der Fake-Broker "handelt" alles, was syntaktisch ein Symbol ist – so kann
        die echte Watchlist unverändert im Synth-Modus getestet werden."""
        return is_valid_symbol_for_broker(normalize_symbol(symbol, "synth"), "stocks", "synth")

    def last_price(self, symbol: str) -> float:
        bars = self.get_bars(symbol, "1h", limit=40)
        return float(bars[-1].close) if bars else 0.0

    # ------------------------------------------------------------------- orders
    def place_order(self, order: Order) -> Order:
        if not self.is_tradable(order.symbol):
            order.status = OrderStatus.REJECTED
            order.error = f"synth broker: '{order.symbol}' ist kein handelbares Symbol"
            return order
        ref = order.entry_reference_price or self.last_price(order.symbol)
        slip = ref * self.slippage_bps / 10_000 * (1 if order.side == Side.BUY else -1)
        price = round(max(1e-9, ref + slip), 6)
        order.broker_order_id = order.broker_order_id or f"synth-{len(self.orders) + 1:06d}"
        order.status = OrderStatus.FILLED
        order.filled_qty = order.qty
        order.avg_fill_price = price
        self.orders[order.broker_order_id] = order
        if not cfg.DRY_RUN:
            self._apply_fill(order, price)
        log.info("[SYNTH] %s %s qty=%.6f @ %.4f (ref %.4f, slip %.1f bps)", order.side.value,
                 order.symbol, order.qty, price, ref, self.slippage_bps)
        return order

    def _apply_fill(self, order: Order, price: float) -> None:
        sign = 1.0 if order.side == Side.BUY else -1.0
        qty = sign * float(order.filled_qty or order.qty)
        pos = self.positions.get(order.symbol)
        if pos is None:
            pos = Position(symbol=order.symbol, qty=0.0, avg_entry=0.0,
                           market=order.market or "stocks", broker=self.name)
            self.positions[order.symbol] = pos
        new_qty = pos.qty + qty
        if abs(new_qty) < 1e-12:
            self.cash += (pos.qty or 0.0) * (order.avg_fill_price or 0.0)
            self.positions.pop(order.symbol, None)
        else:
            if abs(pos.qty) < 1e-12:
                pos.avg_entry = price
            else:
                pos.avg_entry = (abs(pos.qty) * pos.avg_entry + abs(qty) * price) / (abs(pos.qty) + abs(qty))
            pos.qty = new_qty
            self.cash -= qty * price
        self.equity = max(0.0, self.cash + sum(abs(p.qty) * (self.last_price(s) or p.avg_entry)
                                               for s, p in self.positions.items()))

    def cancel_order(self, broker_order_id: str) -> bool:
        return self.orders.pop(broker_order_id, None) is not None

    def get_order(self, broker_order_id: str) -> Order:
        return self.orders.get(broker_order_id, Order(
            broker=self.name, symbol="", side=Side.BUY, qty=0,
            status=OrderStatus.UNKNOWN, error=f"unknown order {broker_order_id}"))

    def status(self) -> dict:
        return {"mode": "synthetic", "equity": self.equity, "cash": self.cash,
                "positions": len(self.positions), "orders": len(self.orders), "seed": self.seed}
