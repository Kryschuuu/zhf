"""Netzwerk-Härtung: Fehlerklassifizierung, Retries, Throttling, Circuit-Breaker.

Warum das existiert: ein nicht auflösbarer Host (z.B. ein erfundener
Testnet-Endpoint) oder ein 429 darf die Pipeline nicht mit identischen
ERROR-Zeilen zuspammen und pro Zyklus Sekunden verbrennen. Ein Broker, der
N-Fehler in Folge liefert, wird für `cfg.BROKER_COOLDOWN_S` nicht mehr
abgefragt; in der Zeit liefern die Adapter leer (== "kein Marktdaten-Beweis"),
und Risk/Research entscheiden konservativ ("kein Signal").
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import cfg

# HTTP-Status, die einen erneuten Versuch lohnen.
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_DNS_MARKERS = (
    "failed to resolve", "nameservice", "nodename nor servname",
    "temporary failure in name resolution", "getaddrinfo", "name or service not known",
)
_CONNECT_MARKERS = (
    "connection refused", "connection reset", "connection aborted",
    "socks connection", "max retries exceeded", "timed out", "timeout",
    "ssl", "tls", "certificate", "network is unreachable",
)


def classify_error(exc: BaseException) -> str:
    """Kurz-Label für Logs/Reports: dns | connect | rate_limit | http | other."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status == 429:
        return "rate_limit"
    if any(m in msg for m in _DNS_MARKERS):
        return "dns"
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return "rate_limit" if status == 429 else "http"
    if any(m in msg for m in _CONNECT_MARKERS):
        return "connect"
    if isinstance(status, int):
        return "http"
    return "other"


def is_transient(exc: BaseException) -> bool:
    """Transient = lohnt einen Retry. DNS-Fehler sind NICHT transient."""
    kind = classify_error(exc)
    if kind == "dns":
        return False
    return kind in ("connect", "rate_limit", "http")


def retry_call(fn: Callable[[], object], *, attempts: Optional[int] = None,
               base_delay: float = 0.5, on_retry: Optional[Callable[[int, Exception], None]] = None):
    """Ausführung mit exponentiellem Backoff + Jitter. Letzter Fehler wird geworfen."""
    n = attempts if attempts is not None else max(1, cfg.HTTP_MAX_RETRIES)
    last: Optional[Exception] = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 – bewusst breit, Klassifikation erfolgt intern
            last = e
            if not is_transient(e) or i == n - 1:
                break
            delay = base_delay * (2 ** i) + random.uniform(0, base_delay)
            if on_retry:
                on_retry(i + 1, e)
            time.sleep(min(delay, 10.0))
    assert last is not None
    raise last


@dataclass
class Breaker:
    """Proportionaler Abbruchschalter pro Broker/Host."""
    name: str
    max_failures: int = field(default_factory=lambda: cfg.BROKER_MAX_CONSECUTIVE_ERRORS)
    cooldown_s: float = field(default_factory=lambda: float(cfg.BROKER_COOLDOWN_S))
    failures: int = 0
    opened_at: float = 0.0
    last_error: str = ""

    @property
    def open(self) -> bool:
        return self.failures >= self.max_failures

    def remaining_s(self) -> float:
        if not self.open:
            return 0.0
        return max(0.0, self.opened_at + self.cooldown_s - time.time())

    def allow(self) -> tuple[bool, str]:
        """(erlaubt, begruendung). Half-Open nach Ablauf der Abkuehlphase."""
        if not self.open:
            return True, ""
        wait = self.remaining_s()
        if wait <= 0:
            # Half-Open: einen Versuch erlauben
            self.failures = self.max_failures - 1
            return True, "half-open"
        return False, f"cooldown {int(wait)}s after {self.failures} errors ({self.last_error})"

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0
        self.last_error = ""

    def record_failure(self, err: object) -> str:
        self.failures += 1
        self.last_error = str(err)[:200]
        if self.open and self.opened_at == 0.0:
            self.opened_at = time.time()
        return self.last_error

    def note(self) -> str:
        return f"{self.name}: {self.failures} Fehler in Folge" + (
            f" (Pause {int(self.remaining_s())}s)" if self.open else ""
        )


_BREAKERS: dict[str, Breaker] = {}


def get_breaker(name: str) -> Breaker:
    if name not in _BREAKERS:
        _BREAKERS[name] = Breaker(name=name)
    return _BREAKERS[name]


def reset_breakers() -> None:
    _BREAKERS.clear()


_LAST_CALL: dict[str, float] = {}


def throttle(key: str, min_interval_s: Optional[float] = None) -> None:
    """Mindestabstand zwischen Aufrufen desselben Keys (Rate-Limit-Schutz)."""
    interval = cfg.ALPACA_DATA_MIN_INTERVAL_S if min_interval_s is None else min_interval_s
    if interval <= 0:
        return
    now = time.monotonic()
    wait = _LAST_CALL.get(key, 0.0) + interval - now
    if wait > 0:
        time.sleep(min(wait, 5.0))
    _LAST_CALL[key] = time.monotonic()


def health_snapshot() -> dict:
    """Für Watchdog/CEO: Zustand aller Circuit-Breaker."""
    out = {}
    for name, b in _BREAKERS.items():
        out[name] = {
            "failures": b.failures,
            "open": b.open,
            "cooldown_remaining_s": round(b.remaining_s(), 1),
            "last_error": b.last_error,
        }
    return out
