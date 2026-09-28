"""Alpaca adapter – stocks, ETFs, crypto via alpaca-py.

Wichtigste Korrekturen:

1.  **Free-Plan / Marktdaten**: ``StockHistoricalDataClient`` fragt ohne Angabe den
    SIP-Feed. Auf dem Gratis-Datenplan kostet der Zugriff auf Daten < 15 min
    ``HTTP 403: subscription does not permit querying recent SIP data`` – genau
    der Fehler aus dem Log. Neu wird der Feed explizit gesetzt (Default ``iex``)
    und bei Bedarf automatisch durchprobiert (``iex`` → ``delayed_sip`` → ``sip``).
2.  **Halbfertige Kerzen**: die letzte Bar einer offenen Periode wird verworfen,
    sonst rechnet Research gegen einen unfertigen Candle.
3.  **Order-TIF**: ``DAY`` ist für Krypto ungültig → ``IOC``/``GTC`` je Assetklasse.
4.  **Fraktional statt Notional**: eine Qty < 1 Aktie wurde als ``notional=0.05``
    (US-Dollar!) geschickt. Jetzt: fraktionale Menge, Notional nur wenn sinnvoll.
5.  **SL/TP**: Risk berechnet Stop-Loss/Take-Profit, die Ausführung hat sie
    ignoriert → OTO-Bracket (Bracket am Broker) wenn unterstützt.
6.  **Robustheit**: Retry bei 429/5xx, Throttling, Circuit-Breaker, klare
    Fehlerklassen statt roher Exception-Strings im Log.
"""
from __future__ import annotations

import re
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
)
from alpaca.trading.enums import OrderSide as AlpacaOrderSide, TimeInForce, OrderClass
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

try:  # ab ~0.16 vorhanden
    from alpaca.data.enums import DataFeed
except Exception:  # pragma: no cover
    DataFeed = None  # type: ignore

try:  # optionale Bracket-Modelle (ältere Versionen: nicht vorhanden)
    from alpaca.trading.requests import TakeProfitRequest, StopLossRequest
except Exception:  # pragma: no cover
    TakeProfitRequest = StopLossRequest = None  # type: ignore


def _stop_loss_request(price: float):
    """alpaca-py benannte das Feld je Version um (stop_price / stop_loss_price)."""
    if StopLossRequest is None:
        return None
    fields = set(getattr(StopLossRequest, "model_fields", {}) or {})
    for key in ("stop_price", "stop_loss_price"):
        if key in fields:
            return StopLossRequest(**{key: round(float(price), 4)})
    return None


def _take_profit_request(price: float):
    if TakeProfitRequest is None:
        return None
    return TakeProfitRequest(limit_price=round(float(price), 4))

from .base import Exchange, Position, Order, Bar, Side, OrderType, OrderStatus
from .symbols import (drop_partial_last_bar, dedupe_bars, is_crypto_symbol,
                      is_valid_symbol, normalize_symbol)
from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.net import classify_error, get_breaker, retry_call, throttle

log = get_logger("alpaca")

_TIMEFRAME_MAP = {
    "1m": TimeFrame(1, TimeFrameUnit.Minute),
    "5m": TimeFrame(5, TimeFrameUnit.Minute),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "30m": TimeFrame(30, TimeFrameUnit.Minute),
    "1h": TimeFrame(1, TimeFrameUnit.Hour),
    "4h": TimeFrame(4, TimeFrameUnit.Hour),
    "1d": TimeFrame(1, TimeFrameUnit.Day),
}

_FEED_MAP = {
    "iex": DataFeed.IEX if DataFeed else "iex",
    "sip": DataFeed.SIP if DataFeed else "sip",
    "delayed_sip": getattr(DataFeed, "DELAYED_SIP", "delayed_sip") if DataFeed else "delayed_sip",
    "otc": getattr(DataFeed, "OTC", "otc") if DataFeed else "otc",
}

# Reihenfolge für "auto": Free-taugliche Feeds zuerst.
_FEED_CHAIN = {"auto": ["iex", "delayed_sip", "sip"],
               "iex": ["iex"], "sip": ["sip"], "delayed_sip": ["delayed_sip"]}

_STOCK_MIN_QTY = 0.0001      # fraktional erlaubt
_CRYPTO_MIN_QTY = 0.0001
_NOTIONAL_RANGE = (1.0, 8000.0)  # Alpaca: $1..$8k pro Notional-Order (Stocks)
_SUBSCRIPTION_MARKERS = ("subscription does not permit", "does not have access", "403")
_INVALID_SYMBOL_MARKERS = ("invalid symbol", "not found", "unknown symbol")


def _feed_value(name: str):
    return _FEED_MAP.get(name, name)


class AlpacaExchange(Exchange):
    name = "alpaca"

    def __init__(self) -> None:
        self.client = TradingClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY,
                                   paper=cfg.ALPACA_PAPER)
        # url_override nur wenn explizit gesetzt (Standard der Clients ist ok)
        self.stock_data = StockHistoricalDataClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY)
        self.crypto_data = CryptoHistoricalDataClient(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY)
        self.breaker = get_breaker(self.name)
        self._feed_working: Optional[str] = None      # positiv erkannter Feed
        self._feed_broken: set[str] = set()            # Feed verworfen (403/Abo)
        self._clock_cache: tuple[float, bool] = (0.0, False)
        self._assets_cache: tuple[float, set[str]] = (0.0, set())
        self._warned_symbols: set[str] = set()
        try:
            acc = self.client.get_account()
            log.info("Alpaca connected. Paper=%s Equity=%s Cash=%s", cfg.ALPACA_PAPER,
                     acc.equity, acc.cash)
        except Exception as e:  # noqa: BLE001
            log.error("Alpaca account check failed (%s): %s", classify_error(e), str(e)[:200])
            self.breaker.record_failure(e)

    # ------------------------------------------------------------------ state
    def get_account(self) -> dict:
        try:
            acc = self.client.get_account()
            fl = lambda k: float(getattr(acc, k, 0) or 0)  # noqa: E731
            return {
                "equity": fl("equity") or fl("last_equity"),
                "cash": fl("cash"),
                "buying_power": fl("buying_power"),
                "currency": getattr(acc, "currency", "USD") or "USD",
            }
        except Exception as e:  # noqa: BLE001
            log.error("Alpaca get_account failed: %s", str(e)[:200])
            return {"equity": 0.0, "cash": 0.0, "buying_power": 0.0, "currency": "USD",
                    "error": str(e)[:200]}

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        try:
            for p in self.client.get_all_positions():
                fl = lambda k: float(getattr(p, k, 0) or 0)  # noqa: E731
                out.append(Position(
                    symbol=p.symbol,
                    qty=fl("qty"),
                    avg_entry=fl("avg_entry_price"),
                    market="crypto" if "/" in p.symbol else "stocks",
                    broker=self.name,
                    unrealized_pnl=fl("unrealized_pl"),
                ))
        except Exception as e:  # noqa: BLE001
            log.error("Alpaca get_positions failed: %s", str(e)[:200])
        return out

    # ------------------------------------------------------------- data layer
    def _supported_feeds(self, is_crypto: bool) -> list[str]:
        if is_crypto:
            return [""]  # Crypto-Feed ist bei Alpaca frei, kein feed-Parameter
        wanted = (cfg.ALPACA_DATA_FEED or "auto").lower()
        chain = _FEED_CHAIN.get(wanted, _FEED_CHAIN["auto"])
        # bekannten funktionierenden Feed vorziehen
        if self._feed_working and self._feed_working in chain:
            chain = [self._feed_working] + [f for f in chain if f != self._feed_working]
        return [f for f in chain if f not in self._feed_broken] or chain[:1]

    @staticmethod
    def _supports_request_feed(req_cls) -> bool:
        try:
            return "feed" in req_cls.model_fields
        except Exception:  # pragma: no cover
            return False

    def get_bars(self, symbol: str, timeframe: str, limit: int = 100) -> list[Bar]:
        sym = normalize_symbol(symbol, self.name)
        market = "crypto" if is_crypto_symbol(sym) else "stocks"
        if not is_valid_symbol(sym, market):
            if sym not in self._warned_symbols:
                self._warned_symbols.add(sym)
                log.warning("Alpaca: '%s' ist kein handelbares Symbol – übersprungen "
                            "(Platzhalter/Test-Symbol?).", symbol)
            return []
        tf = _TIMEFRAME_MAP.get((timeframe or "1h").lower(), TimeFrame(1, TimeFrameUnit.Hour))
        is_crypto = market == "crypto"
        end = datetime.now(timezone.utc)
        # 1h/1d brauchen Vorlauf: Start so wählen, dass `limit` Kerzen realistisch sind.
        span = max(timedelta(hours=2) * limit, timedelta(days=2)) if (timeframe or "1h") != "1d" \
            else timedelta(days=max(30, limit + 5))
        start = end - span
        last_err: Optional[Exception] = None
        for feed in self._supported_feeds(is_crypto):
            try:
                bars = self._fetch_bars(sym, tf, start, end, limit, is_crypto, feed)
                if bars:
                    self._feed_working = feed or self._feed_working
                    self.breaker.record_success()
                    if cfg.ALPACA_DROP_PARTIAL_BAR:
                        bars = drop_partial_last_bar(bars, timeframe or "1h", end)
                    return dedupe_bars(bars)[-limit:]
                # leerer, aber gültiger Response: Feed akzeptiert, keine Daten
                if feed and "iex" in feed:
                    log.debug("Alpaca %s: 0 Bars via IEX-Feed (IEX führt das Symbol evtl. nicht)", sym)
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).lower()
                if any(m in msg for m in _INVALID_SYMBOL_MARKERS):
                    log.warning("Alpaca: Symbol '%s' nicht handelbar/ungültig: %s",
                                sym, str(e)[:120].replace("\n", " "))
                    return []
                if any(m in msg for m in _SUBSCRIPTION_MARKERS):
                    self._feed_broken.add(feed)
                    log.warning("Alpaca Feed '%s' für %s nicht berechtigt (%s) – versuche nächsten Feed. "
                                "Dauerhaft: ALPACA_DATA_FEED=iex lassen oder Abo buchen.",
                                feed or "crypto", sym, classify_error(e))
                    continue
                kind = classify_error(e)
                if kind == "dns":
                    self.breaker.record_failure(e)
                    break
                log.warning("Alpaca get_bars(%s, feed=%s) fehlgeschlagen (%s): %s",
                            sym, feed or "-", kind, str(e)[:160].replace("\n", " "))
        if last_err is not None:
            self.breaker.record_failure(last_err)
            log.error("Alpaca get_bars(%s) endgültig fehlgeschlagen (%s): %s",
                      sym, classify_error(last_err), str(last_err)[:180].replace("\n", " "))
        return []

    def _fetch_bars(self, sym: str, tf: TimeFrame, start: datetime, end: datetime,
                    limit: int, is_crypto: bool, feed: str) -> list[Bar]:
        throttle(f"alpaca:{sym}", cfg.ALPACA_DATA_MIN_INTERVAL_S)
        allowed, why = self.breaker.allow()
        if not allowed:
            log.debug("Alpaca übersprungen (%s): %s", sym, why)
            return []

        def do():
            if is_crypto:
                req = CryptoBarsRequest(symbol_or_symbols=[sym], timeframe=tf,
                                        start=start, end=end, limit=max(limit, 30))
                return self.crypto_data.get_crypto_bars(req)
            kwargs: dict[str, Any] = {"symbol_or_symbols": [sym], "timeframe": tf,
                                      "start": start, "end": end, "limit": max(limit, 30)}
            if feed and self._supports_request_feed(StockBarsRequest):
                kwargs["feed"] = _feed_value(feed)
            req = StockBarsRequest(**kwargs)
            return self.stock_data.get_stock_bars(req)

        resp = retry_call(do, attempts=cfg.ALPACA_MAX_RETRIES,
                          on_retry=lambda i, e: (log.warning("Alpaca %s: Retry %d (%s)", sym, i, str(e)[:120]),
                                                 _time.sleep(0.6 * i))[0])
        return self._bars_from_response(resp, sym)

    @staticmethod
    def _bars_from_response(resp: Any, symbol: str) -> list[Bar]:
        try:
            df = resp.df
        except Exception:  # raw dict responses
            return []
        if df is None or getattr(df, "empty", True):
            return []
        out: list[Bar] = []
        try:
            flat = df.reset_index()
        except Exception:  # pragma: no cover
            return []
        for _, row in flat.iterrows():
            try:
                ts = row.get("timestamp", None)
                if ts is None:
                    for cand in ("time", "date"):
                        if row.get(cand) is not None:
                            ts = row.get(cand)
                            break
                close = row.get("close")
                if close is None or (isinstance(close, float) and close != close):
                    continue  # NaN
                if hasattr(ts, "to_pydatetime"):
                    ts = ts.to_pydatetime()
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                out.append(Bar(
                    symbol=symbol, timestamp=ts, open=float(row.get("open") or 0),
                    high=float(row.get("high") or 0), low=float(row.get("low") or 0),
                    close=float(close), volume=float(row.get("volume") or 0),
                ))
            except Exception as e:  # noqa: BLE001
                log.debug("Bar-Zeile übersprungen (%s): %s", e, row)
                continue
        return out

    # ------------------------------------------------------------ order layer
    def _is_crypto(self, symbol: str) -> bool:
        return is_crypto_symbol(symbol)

    def place_order(self, order: Order) -> Order:
        if cfg.DRY_RUN:
            ref = order.avg_fill_price or order.limit_price or order.entry_reference_price or 0.0
            log.info("[DRY_RUN] Alpaca: %s %s qty=%.6f (ref=%.4f)", order.side.value,
                     order.symbol, order.qty, ref)
            order.status = OrderStatus.FILLED
            order.filled_qty = order.qty
            order.avg_fill_price = order.avg_fill_price or round(float(ref), 6)
            order.broker_order_id = f"dry-{order.client_order_id or int(_time.time())}"
            return order

        sym = normalize_symbol(order.symbol, self.name)
        crypto = self._is_crypto(sym)
        if not is_valid_symbol(sym, "crypto" if crypto else "stocks"):
            order.status = OrderStatus.REJECTED
            order.error = f"invalid symbol for Alpaca: {order.symbol}"
            return order

        side = AlpacaOrderSide.BUY if order.side == Side.BUY else AlpacaOrderSide.SELL
        qty = float(order.qty)
        # TIF: fuer Krypto ist DAY ungueltig (Alpaca akzeptiert dort nur GTC/IOC).
        tif = TimeInForce.IOC if crypto else (
            TimeInForce.DAY if not cfg.ALPACA_EXTENDED_HOURS else TimeInForce.IOC)
        client_id = order.client_order_id or f"zhf-{int(_time.time())}"

        size_kwargs: dict[str, Any]
        min_qty = _CRYPTO_MIN_QTY if crypto else _STOCK_MIN_QTY
        if qty >= min_qty:
            size_kwargs = {"qty": round(qty, 8 if crypto else 6)}
        elif not crypto and _NOTIONAL_RANGE[0] <= qty <= _NOTIONAL_RANGE[1]:
            # Qty < 1 Aktie: nur als Dollar-Notional sinnvoll (Alpaca: $1..$8000).
            size_kwargs = {"notional": round(qty, 2)}
        else:
            order.status = OrderStatus.REJECTED
            order.error = (f"order size too small for Alpaca (qty={qty}); min {min_qty} shares "
                           f"or ${_NOTIONAL_RANGE[0]:.0f}-${_NOTIONAL_RANGE[1]:.0f} notional")
            log.error("Alpaca order rejected: %s %s → %s", order.side.value, sym, order.error)
            return order

        bracket_ok = (cfg.ALPACA_BRACKET_ORDERS and not crypto
                      and bool(TakeProfitRequest) and bool(StopLossRequest)
                      and order.stop_loss and order.take_profit and order.type == OrderType.MARKET)
        extra: dict[str, Any] = {"time_in_force": tif, "client_order_id": client_id}
        if not crypto:
            extra["extended_hours"] = bool(cfg.ALPACA_EXTENDED_HOURS)
        if bracket_ok:
            tp_req = _take_profit_request(order.take_profit)
            sl_req = _stop_loss_request(order.stop_loss)
            if tp_req is None or sl_req is None:
                log.warning("Alpaca: Bracket-Modelle dieser alpaca-py-Version nicht "
                            "unterstützt – SL/TP nur intern überwacht")
            else:
                extra.update({"order_class": OrderClass.OTO, "take_profit": tp_req,
                              "stop_loss": sl_req})

        def build():
            if order.type == OrderType.MARKET:
                return MarketOrderRequest(symbol=sym, side=side, **size_kwargs, **extra)
            if not order.limit_price:
                raise ValueError("LIMIT order requires limit_price")
            return LimitOrderRequest(symbol=sym, side=side, limit_price=float(order.limit_price),
                                     time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
                                     client_order_id=client_id, **size_kwargs)

        try:
            res = self.client.submit_order(build())
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if bracket_ok and re.search(r"order_class|bracket|OTO|take_profit|stop_loss", msg, re.I):
                log.warning("Alpaca: OTO-Bracket abgelehnt, wiederhole ohne SL/TP: %s", msg[:160])
                for k in ("order_class", "take_profit", "stop_loss"):
                    extra.pop(k, None)
                try:
                    res = self.client.submit_order(build())
                    order.broker_order_id = str(res.id)
                    order.status = OrderStatus.NEW
                    order.error = "bracket not supported by account - SL/TP enforced by the bot only"
                    log.warning("Order %s ohne Broker-SL/TP platziert – Exit-Logik ist Pflicht!",
                                order.broker_order_id)
                    return order
                except Exception as e2:  # noqa: BLE001
                    msg = str(e2)
            order.status = OrderStatus.REJECTED
            order.error = msg[:300]
            self.breaker.record_failure(e)
            log.error("Alpaca place_order failed (%s): %s", classify_error(e), msg[:250].replace("\n", " "))
            return order
        order.broker_order_id = str(res.id)
        order.status = OrderStatus.NEW
        self.breaker.record_success()
        log.info("Alpaca order %s submitted: %s %s %s", order.broker_order_id, order.side.value,
                 sym, size_kwargs)
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(broker_order_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("Cancel order %s failed: %s", broker_order_id, str(e)[:200])
            return False

    _STATUS_MAP = {
        "new": OrderStatus.NEW, "accepted": OrderStatus.NEW, "pending_new": OrderStatus.NEW,
        "partially_filled": OrderStatus.PARTIAL, "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED, "cancelled": OrderStatus.CANCELED,
        "expired": OrderStatus.CANCELED, "rejected": OrderStatus.REJECTED,
        "replaced": OrderStatus.NEW,
    }

    def get_order(self, broker_order_id: str) -> Order:
        try:
            o = self.client.get_order_by_id(broker_order_id)
            fl = lambda k: float(getattr(o, k, 0) or 0)  # noqa: E731
            return Order(
                broker=self.name,
                symbol=getattr(o, "symbol", "") or "",
                side=Side.BUY if str(getattr(o, "side", "buy")).lower() == "buy" else Side.SELL,
                qty=fl("qty"),
                filled_qty=fl("filled_qty"),
                avg_fill_price=fl("filled_avg_price"),
                broker_order_id=str(o.id),
                status=self._STATUS_MAP.get(str(getattr(o, "status", "")).lower(), OrderStatus.UNKNOWN),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("get_order %s failed: %s", broker_order_id, str(e)[:160])
            return Order(broker=self.name, symbol="", side=Side.BUY, qty=0,
                         status=OrderStatus.UNKNOWN, error=str(e)[:200])

    # ---------------------------------------------------------- market status
    def is_market_open(self, symbol: str) -> bool:
        if self._is_crypto(symbol):
            return True  # Krypto 24/7
        now = _time.time()
        if now - self._clock_cache[0] > 60:
            try:
                clock = self.client.get_clock()
                self._clock_cache = (now, bool(clock.is_open))
            except Exception as e:  # noqa: BLE001
                log.warning("Alpaca clock failed: %s", str(e)[:150])
                # Bei Unknown konservativ "geschlossen" → keine Order statt Blindflug
                self._clock_cache = (now, False)
        return self._clock_cache[1]

    def _known_assets(self) -> set[str]:
        now = _time.time()
        if now - self._assets_cache[0] > 3600:
            try:
                assets = self.client.get_all_assets()
                self._assets_cache = (now, {a.symbol for a in assets})
            except Exception as e:  # noqa: BLE001
                log.debug("Alpaca asset list unavailable (%s) – Syntax-Check genügt", str(e)[:120])
                self._assets_cache = (now, set())
        return self._assets_cache[1]

    def is_tradable(self, symbol: str) -> bool:
        """Syntaktisch gültig UND (wenn bekannt) bei Alpaca gelistet."""
        sym = normalize_symbol(symbol, self.name)
        if not is_valid_symbol(sym, "crypto" if self._is_crypto(sym) else "stocks"):
            return False
        known = self._known_assets()
        if not known:
            return True
        return sym in known

    def data_feed_status(self) -> dict:
        return {"requested": cfg.ALPACA_DATA_FEED, "working_feed": self._feed_working,
                "rejected_feeds": sorted(self._feed_broken), "breaker": self.breaker.note(),
                "last_error": self.breaker.last_error}

    def status(self) -> dict:
        return self.data_feed_status()
