"""CCXT-basierter Adapter für BingX (Spot + USDT-M Perps).

Bitunix ist in CCXT NICHT integriert – dafür existiert der eigene Adapter in
``bitunix_exchange``.

Korrekturen:

1.  **Sandbox wurde nie aktiviert**: ``params["sandboxMode"]`` ist kein CCXT-
    Initialisierungsschlüssel – die Einstellung verpuffte lautlos, es lief
    Production trotz ``testnet=True`` im Log. Richtig ist
    ``exchange.set_sandbox_mode(True)`` (BingX → ``open-api-vst``).
2.  **Symbolform**: lineare Perps heissen in CCXT ``BTC/USDT:USDT``. Nacktes
    ``BTC/USDT`` führt zu "does not have market symbol". Neu wird gegen die
    geladenen Markets aufgelöst (mit Fallback).
3.  **Kline-Limit**, Throttling, Retry auf transienten Fehlern,
    Circuit-Breaker und saubere Fehlerklassen.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

import ccxt

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from .symbols import (ccxt_swap_symbol, dedupe_bars, drop_partial_last_bar,
                      is_valid_symbol, normalize_symbol, to_exchange_symbol)
from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.net import classify_error, get_breaker, retry_call, throttle

log = get_logger("ccxt")

_CCXT_TIMEFRAME = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
                   "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h",
                   "1d": "1d", "1w": "1w"}


class CCXTExchange(Exchange):
    def __init__(self, exchange_id: str, api_key: str, secret: str, testnet: bool,
                 market_type: str = "swap"):
        self.exchange_id = exchange_id
        self.market_type = (market_type or "swap").lower()
        if not hasattr(ccxt, exchange_id):
            raise ImportError(f"ccxt hat keinen Exchange '{exchange_id}' – ccxt updaten "
                              f"pip install -U 'ccxt>=4.0'")
        cls = getattr(ccxt, exchange_id)
        params: dict[str, Any] = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,           # ccxt-eigenes Throttling respektieren
            "options": {"defaultType": market_type},
            "timeout": int(cfg.HTTP_TIMEOUT_S * 1000),
        }
        if cfg.BINGX_BASE_URL and exchange_id == "bingx":
            params["urls"] = {"api": {"spot": cfg.BINGX_BASE_URL, "swap": cfg.BINGX_BASE_URL,
                                      "contract": cfg.BINGX_BASE_URL, "linear": cfg.BINGX_BASE_URL}}
        self.ex = cls(params)
        self.breaker = get_breaker(exchange_id)
        self.read_only = not bool(api_key and secret)
        self.testnet_applied = False
        if testnet:
            self.testnet_applied = self._enable_sandbox()
        # Sicherheitsregel: Testnet verlangt, aber nicht erreichbar -> keine
        # Live-Orders. Lieber steht die Anlage still, als dass "im Test" echt
        # gehandelt wird.
        self.block_live = bool(testnet) and not self.testnet_applied
        if self.read_only:
            log.warning("%s ohne API-Keys gestartet – nur öffentliche Marktdaten, Orders blockiert.",
                        exchange_id)
        self._markets_ready = False
        try:
            self.ex.load_markets()
            self._markets_ready = True
            log.info("%s: %d markets loaded (testnet=%s%s)", exchange_id, len(self.ex.markets),
                     testnet, "" if not testnet else (", aktiv" if self.testnet_applied else ", NICHT aktiv!"))
        except Exception as e:  # noqa: BLE001
            kind = classify_error(e)
            log.warning("%s load_markets failed (%s): %s – Public-Endpunkte werden "
                        "trotzdem versucht.", exchange_id, kind, str(e)[:180])
            self.breaker.record_failure(e)

    def _enable_sandbox(self) -> bool:
        """CCXT-Testnet korrekt einschalten und verifizieren."""
        try:
            if not getattr(self.ex, "urls", {}).get("test"):
                log.error("%s bietet in ccxt KEIN Testnet an (urls['test'] fehlt). "
                          "BINGX_TESTNET/…_TESTNET=true hat keine Wirkung – bitte Demo-Subkonto "
                          "oder DRY_RUN=true nutzen.", self.exchange_id)
                return False
            self.ex.set_sandbox_mode(True)
            base = self.ex.urls.get("api")
            if isinstance(base, dict):
                base = next(iter(base.values()), "")
            log.info("%s sandbox aktiv: %s", self.exchange_id, str(base)[:120])
            return True
        except Exception as e:  # noqa: BLE001
            log.error("%s: Sandbox konnte nicht aktiviert werden (%s) – Adapter bleibt "
                      "auf PRODUCTION, Orders nur wenn DRY_RUN=false! %s",
                      self.exchange_id, classify_error(e), str(e)[:150])
            return False

    @property
    def name(self) -> str:
        return self.exchange_id

    # ------------------------------------------------------------------ helpers
    def _resolve_symbol(self, symbol: str) -> Optional[str]:
        """Symbol gegen die geladenen Markets validieren/normalisieren."""
        s = normalize_symbol(symbol, self.exchange_id)
        if not is_valid_symbol(s, "crypto_perp"):
            log.warning("%s: '%s' ist kein gültiges Perp-Symbol – übersprungen.", self.exchange_id, symbol)
            return None
        if not self._markets_ready:
            return s
        markets = self.ex.markets
        # Spot und Perp desselben Paares koennen beide existieren ("BTC/USDT" und
        # "BTC/USDT:USDT"). Wer Perps handeln will, darf nicht versehentlich die
        # Spot-Kerzen/Spot-Order erwischen ->先看 defaultType.
        default_type = str((getattr(self.ex, "options", {}) or {}).get("defaultType")
                           or self.market_type).lower()
        if default_type in ("swap", "linear", "future", "contract"):
            cands = (ccxt_swap_symbol(s), f"{s}:{'USDT' if s.endswith('USDT') else 'USD'}",
                     to_exchange_symbol(s, "bitunix"), s)
        else:
            cands = (s, to_exchange_symbol(s, "bitunix"))
        for cand in cands:
            if cand and cand in markets:
                return cand
        log.warning("%s: Symbol '%s' nicht in %d Markets gefunden – prüfe Watchlist/Broker.",
                    self.exchange_id, s, len(markets))
        return None

    def _call(self, fn, *args, **kwargs):
        allowed, why = self.breaker.allow()
        if not allowed:
            log.debug("%s Call übersprungen: %s", self.exchange_id, why)
            return None
        throttle(f"{self.exchange_id}", 0.15)

        def do():
            return fn(*args, **kwargs)
        try:
            out = retry_call(do, attempts=cfg.HTTP_MAX_RETRIES,
                             on_retry=lambda i, e: log.warning("%s retry %d: %s",
                                                               self.exchange_id, i, str(e)[:140]))
            self.breaker.record_success()
            return out
        except Exception as e:  # noqa: BLE001
            self.breaker.record_failure(e)
            log.error("%s %s failed (%s): %s", self.exchange_id, getattr(fn, "__name__", "call"),
                      classify_error(e), str(e)[:200])
            return None

    # ------------------------------------------------------------------ API
    def get_account(self) -> dict:
        if self.read_only:
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": "USDT",
                    "error": "no api keys (data-only mode)"}
        data = self._call(self.ex.fetch_balance)
        if not data:
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": "USDT",
                    "error": self.breaker.last_error or "balance unavailable"}
        usdt = data.get("USDT") or data.get("total", {}).get("USDT") or {}
        total = float((usdt or {}).get("total", 0) or 0)
        free = float((usdt or {}).get("free", 0) or 0)
        return {"equity": total, "cash": free, "buying_power": free, "currency": "USDT"}

    def get_positions(self) -> list[Position]:
        if self.read_only:
            return []
        rows = self._call(self.ex.fetch_positions) or []
        out: list[Position] = []
        for p in rows:
            try:
                qty = float(p.get("contracts") or 0)
                if qty == 0:
                    continue
                out.append(Position(
                    symbol=normalize_symbol(str(p.get("symbol", "")), self.exchange_id),
                    qty=qty if str(p.get("side", "long")) == "long" else -qty,
                    avg_entry=float(p.get("entryPrice") or 0),
                    market="crypto_perp",
                    broker=self.exchange_id,
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                    leverage=int(float(p.get("leverage") or 1)),
                ))
            except (TypeError, ValueError) as e:
                log.debug("%s position row skipped: %s", self.exchange_id, e)
        return out

    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        sym = self._resolve_symbol(symbol)
        if not sym:
            return []
        tf = _CCXT_TIMEFRAME.get((timeframe or "1h").lower(), "1h")
        limit = max(5, min(int(limit or 100), getattr(self.ex, "limit", 1000) or 500))
        ohlcv = self._call(self.ex.fetch_ohlcv, sym, timeframe=tf, limit=limit) or []
        bars: list[Bar] = []
        for row in ohlcv:
            try:
                ts, o, h, l, c, v = row[:6]
                bars.append(Bar(
                    symbol=normalize_symbol(symbol, self.exchange_id),
                    timestamp=datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc),
                    open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0),
                ))
            except (TypeError, ValueError) as e:
                log.debug("%s ohlcv row skipped (%s): %s", self.exchange_id, e, row)
        if cfg.ALPACA_DROP_PARTIAL_BAR:
            bars = drop_partial_last_bar(bars, timeframe or "1h")
        return dedupe_bars(bars)[-limit:]

    def place_order(self, order: Order) -> Order:
        if cfg.DRY_RUN:
            log.info("[DRY_RUN] %s: %s %s %.6f %s", self.name, order.side.value, order.symbol,
                     order.qty, order.type.value)
            order.status = OrderStatus.FILLED
            order.filled_qty = order.qty
            order.avg_fill_price = order.avg_fill_price or float(order.limit_price
                                                                or order.entry_reference_price or 0.0)
            order.broker_order_id = f"dry-{order.client_order_id or int(time.time())}"
            return order
        if self.read_only:
            order.status = OrderStatus.REJECTED
            order.error = f"{self.exchange_id}: keine API-Keys konfiguriert (data-only) – Order blockiert"
            log.error("%s order blocked: %s %s", self.exchange_id, order.side.value, order.symbol)
            return order
        if self.block_live:
            order.status = OrderStatus.REJECTED
            order.error = (f"{self.exchange_id}: *_TESTNET=true, aber Sandbox nicht verfügbar – "
                           f"Live-Orders gesperrt. Testnet-Keys hinterlegen oder TESTNET=false + DRY_RUN=false")
            log.error("%s order blocked: %s", self.exchange_id, order.error)
            return order
        sym = self._resolve_symbol(order.symbol)
        if not sym:
            order.status = OrderStatus.REJECTED
            order.error = f"{order.symbol} not tradable on {self.exchange_id}"
            return order
        side = "buy" if order.side == Side.BUY else "sell"
        otype = "market" if order.type == OrderType.MARKET else "limit"
        params: dict[str, Any] = {}
        if order.stop_loss:
            params["stopLoss"] = {"triggerPrice": float(order.stop_loss)}
        if order.take_profit:
            params["takeProfit"] = {"triggerPrice": float(order.take_profit)}
        if order.leverage and order.leverage > 1:
            params["leverage"] = int(order.leverage)
        if order.client_order_id:
            params["clientOrderId"] = order.client_order_id[:32]
        res = self._call(self.ex.create_order, sym, otype, side, order.qty,
                         order.limit_price, params or None)
        if not res:
            order.status = OrderStatus.REJECTED
            order.error = self.breaker.last_error or "no response from exchange"
            return order
        order.broker_order_id = str(res.get("id", "") or "") or None
        status = str(res.get("status", "")).lower()
        if status == "closed":
            order.status = OrderStatus.FILLED
            order.filled_qty = float(res.get("filled") or order.qty)
            order.avg_fill_price = float(res.get("average") or res.get("price") or 0)
        else:
            order.status = OrderStatus.NEW
        return order

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        sym = self._resolve_symbol(symbol)
        if not sym or self.read_only:
            return False
        res = self._call(self.ex.set_leverage, leverage, sym)
        return bool(res)

    def cancel_order(self, broker_order_id: str, symbol: str = "") -> bool:
        if self.read_only:
            return False
        res = self._call(self.ex.cancel_order, broker_order_id, symbol or None)
        return bool(res)

    def get_order(self, broker_order_id: str) -> Order:
        if self.read_only or not broker_order_id:
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0,
                         status=OrderStatus.UNKNOWN, error="data-only mode")
        o = self._call(self.ex.fetch_order, broker_order_id)
        if not o:
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0,
                         status=OrderStatus.UNKNOWN, error=self.breaker.last_error or "not found")
        status_map = {"open": OrderStatus.NEW, "closed": OrderStatus.FILLED,
                      "canceled": OrderStatus.CANCELED, "cancelled": OrderStatus.CANCELED,
                      "rejected": OrderStatus.REJECTED, "expired": OrderStatus.CANCELED}
        return Order(
            broker=self.name,
            symbol=normalize_symbol(str(o.get("symbol", "")), self.exchange_id),
            side=Side.BUY if str(o.get("side", "buy")).lower() == "buy" else Side.SELL,
            qty=float(o.get("amount") or 0),
            filled_qty=float(o.get("filled") or 0),
            avg_fill_price=float(o.get("average") or o.get("price") or 0),
            broker_order_id=str(o.get("id") or broker_order_id),
            status=status_map.get(str(o.get("status", "")).lower(), OrderStatus.UNKNOWN),
        )

    def is_market_open(self, symbol: str) -> bool:
        return not self.breaker.open  # Krypto 24/7 – ausser die Boerse ist nicht erreichbar

    def is_tradable(self, symbol: str) -> bool:
        sym = self._resolve_symbol(symbol)
        if not sym:
            return False
        if not self._markets_ready:
            return True
        info = self.ex.markets.get(sym) or {}
        return bool(info.get("active", True))

    def status(self) -> dict:
        return {"testnet_requested": (cfg.BINGX_TESTNET if self.exchange_id == "bingx" else False),
                "testnet_active": self.testnet_applied, "read_only": self.read_only,
                "live_orders_blocked": self.block_live,
                "markets_loaded": self._markets_ready, "breaker": self.breaker.note(),
                "last_error": self.breaker.last_error}


def make_bingx() -> CCXTExchange:
    return CCXTExchange("bingx", cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY, cfg.BINGX_TESTNET)


def make_bitunix() -> CCXTExchange:
    """Nur relevant, falls ein künftiges ccxt Bitunix integriert."""
    return CCXTExchange("bitunix", cfg.BITUNIX_API_KEY, cfg.BITUNIX_SECRET_KEY, cfg.BITUNIX_TESTNET)
