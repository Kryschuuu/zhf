"""Factory – lazy-load aller konfigurierten Broker."""
from __future__ import annotations

from typing import Optional

from .base import Exchange
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("factory")

_cache: dict[str, Optional[Exchange]] = {}


def _has_keys(k: str, s: str) -> bool:
    return bool(k and s and "your_" not in k)


def get_exchanges() -> dict[str, Exchange]:
    """Liefert alle konfigurierten + erreichbaren Broker als Dict name → Exchange."""
    global _cache
    if _cache:
        return {k: v for k, v in _cache.items() if v is not None}  # type: ignore

    # Alpaca
    if _has_keys(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY):
        try:
            from .alpaca_exchange import AlpacaExchange
            _cache["alpaca"] = AlpacaExchange()
        except Exception as e:
            log.warning("Alpaca init failed: %s", e)
            _cache["alpaca"] = None
    else:
        log.info("Alpaca keys not configured – skipping.")

    # BingX
    if _has_keys(cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY):
        try:
            from .ccxt_exchange import make_bingx
            _cache["bingx"] = make_bingx()
        except Exception as e:
            log.warning("BingX init failed: %s", e)
            _cache["bingx"] = None
    else:
        log.info("BingX keys not configured – skipping.")

    # Bitunix
    if _has_keys(cfg.BITUNIX_API_KEY, cfg.BITUNIX_SECRET_KEY):
        try:
            from .bitunix_exchange import BitunixExchange
            _cache["bitunix"] = BitunixExchange()
        except Exception as e:
            log.warning("Bitunix init failed: %s", e)
            _cache["bitunix"] = None
    else:
        log.info("Bitunix keys not configured – skipping.")

    return {k: v for k, v in _cache.items() if v is not None}  # type: ignore


def reset_cache() -> None:
    global _cache
    _cache = {}
