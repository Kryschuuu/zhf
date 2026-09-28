"""Factory – lazy-load aller konfigurierten Broker.

`ZHF_SYNTH=1` schaltet den rein lokalen Synth-Broker scharf (keine API-Keys,
kein Netz, keine echten Orders) – für End-to-End-Tests der Pipeline.
"""
from __future__ import annotations

from typing import Optional

from .base import Exchange
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("factory")

_cache: dict[str, Optional[Exchange]] = {}
_unavailable: dict[str, str] = {}


def _has_keys(k: str, s: str) -> bool:
    return bool(k and s and "your_" not in k and len(k) >= 8)


def _missing_reason(name: str, key: str, secret: str) -> str:
    if not key or not secret:
        return f"{name}: API-Keys fehlen (BITUNIX/BINGX/ALPACA *_API_KEY / *_SECRET_KEY in .env)"
    if "your_" in key:
        return f"{name}: API-Key ist noch der Platzhalter 'your_…' aus .env.example"
    return f"{name}: Key/Secret unvollständig (Key-Länge {len(key)})"


def _register(name: str, factory) -> None:
    try:
        ex = factory()
        if ex is None:
            _cache[name] = None
            _unavailable[name] = f"{name}: Adapter lieferte None"
            return
        _cache[name] = ex
        log.info("Broker aktiv: %s", name)
    except Exception as e:  # noqa: BLE001
        log.warning("Broker %s initialisierung fehlgeschlagen: %s", name, str(e)[:200])
        _cache[name] = None
        _unavailable[name] = f"{name}: {str(e)[:160]}"


def get_exchanges() -> dict[str, Exchange]:
    """Liefert alle konfigurierten + erreichbaren Broker als Dict name → Exchange."""
    if _cache:
        return {k: v for k, v in _cache.items() if v is not None}  # type: ignore[assignment]

    if cfg.SYNTH:
        from .synth_exchange import SynthExchange
        _register("synth", SynthExchange)
        return {k: v for k, v in _cache.items() if v is not None}  # type: ignore[assignment]

    # Alpaca (Stocks/ETFs/Crypto) – Marktdaten auch ohne Paper-Geld sinnvoll
    if _has_keys(cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY):
        from .alpaca_exchange import AlpacaExchange
        _register("alpaca", AlpacaExchange)
    else:
        _unavailable["alpaca"] = _missing_reason("Alpaca", cfg.ALPACA_API_KEY, cfg.ALPACA_SECRET_KEY)
        log.info("%s", _unavailable["alpaca"])

    # BingX via CCXT
    if _has_keys(cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY):
        from .ccxt_exchange import make_bingx
        _register("bingx", make_bingx)
    else:
        _unavailable["bingx"] = _missing_reason("BingX", cfg.BINGX_API_KEY, cfg.BINGX_SECRET_KEY)
        log.info("%s", _unavailable["bingx"])

    # Bitunix via eigenem REST-Adapter (nicht in CCXT)
    if _has_keys(cfg.BITUNIX_API_KEY, cfg.BITUNIX_SECRET_KEY):
        from .bitunix_exchange import BitunixExchange
        _register("bitunix", BitunixExchange)
    else:
        _unavailable["bitunix"] = _missing_reason("Bitunix", cfg.BITUNIX_API_KEY, cfg.BITUNIX_SECRET_KEY)
        log.info("%s", _unavailable["bitunix"])

    return {k: v for k, v in _cache.items() if v is not None}  # type: ignore[assignment]


def unavailable_brokers() -> dict[str, str]:
    """Grund, warum ein Broker nicht geladen wurde – für Watchdog/Setup-Check."""
    get_exchanges()
    return dict(_unavailable)


def broker_diagnostics() -> dict:
    """Status aller Adapter (Feeds, Circuit-Breaker, Testnet-Modus …)."""
    brokers: dict[str, dict] = {}
    for name, ex in get_exchanges().items():
        try:
            brokers[name] = ex.status() or {}
        except Exception as e:  # noqa: BLE001
            brokers[name] = {"error": str(e)[:150]}
    return {"brokers": brokers, "unavailable": dict(_unavailable)}


def reset_cache() -> None:
    global _cache, _unavailable
    _cache = {}
    _unavailable = {}
    try:
        from scripts.common.net import reset_breakers
        reset_breakers()
    except Exception:  # pragma: no cover
        pass
