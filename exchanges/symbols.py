"""Symbol-Normalisierung und -Validierung über alle Broker hinweg.

Hintergrund: über die JSON-State-Dateien (candidates/validated/approved) können
Symbole landen, die kein echter Handelsgegenstand sind – z.B. die Platzhalter
`SYNTH_*` aus dem Pipeline-Selbsttest. Ungeprüft gehen sie als Order-Request an
den Broker ("invalid symbol") oder erzeugen im Risk-Agent teure, nutzlose
Data-Calls. Diese Modul prüft deshalb VOR jedem Netzwerkaufruf.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Bekannte Krypto-Basiswerte (Alpaca-Aktiv-Symbole ohneSlash-Schreibweise)
CRYPTO_BASES = {"BTC", "ETH", "SOL", "DOGE", "XRP", "LTC", "ADA", "AVAX", "DOT", "LINK", "MATIC"}

# US- Aktien/ETF-Symbole: Buchstaben, Punkt, Bindestrich, Slash (Klassen-Aktien)
STOCK_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
# Crypto-Paar bei Alpaca: BASE/QUOTE
CRYPTO_PAIR_RE = re.compile(r"^[A-Z]{2,10}/[A-Z]{2,5}$")
# Crypto-Pair auf Perps-Börsen: BASE/QUOTE oder kompaktes BASEQUOTE (BTCUSDT)
PERP_RE = re.compile(r"^[A-Z0-9]{2,12}[/_]?[A-Z0-9]{2,6}(:[A-Z0-9]{2,6})?$")

# Alles, was nach Test-/Platzhalterdaten aussieht, ist kein handelbares Symbol.
_PLACEHOLDER_RE = re.compile(r"(SYNTH|TEST|DEMO|FAKE|EXAMPLE|YOUR_|XXX|TBD|N/?A)", re.IGNORECASE)

_TIMEDELTA = {
    "1m": timedelta(minutes=1), "3m": timedelta(minutes=3), "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15), "30m": timedelta(minutes=30), "1h": timedelta(hours=1),
    "2h": timedelta(hours=2), "4h": timedelta(hours=4), "6h": timedelta(hours=6),
    "12h": timedelta(hours=12), "1d": timedelta(days=1), "1w": timedelta(weeks=1),
}


def timeframe_delta(timeframe: str) -> timedelta:
    return _TIMEDELTA.get((timeframe or "1h").lower(), timedelta(hours=1))


def is_placeholder_symbol(symbol: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(symbol or ""))


def normalize_symbol(symbol: str, broker: str = "") -> str:
    """Broker-neutrale Schreibweise: gross, Leerzeichen weg.

    `BTCUSDT` → `BTC/USDT` (Perps), Alpaca-Spot → `BTC/USD`.
    """
    s = (symbol or "").strip().upper()
    broker = (broker or "").lower()
    if not s:
        return s
    if "/" not in s and broker in ("bingx", "bitunix", "synth"):
        m = re.match(r"^([A-Z0-9]{2,10})(USDT|USDC|USD|BUSDT)$", s)
        if m:
            quote = m.group(2)
            quote = "USDT" if quote in ("USDT", "BUSDT") else "USD"
            return f"{m.group(1)}/{quote}"
    return s


def to_exchange_symbol(symbol: str, broker: str) -> str:
    """Vom Broker erwartete Schreibweise."""
    s = normalize_symbol(symbol, broker)
    broker = (broker or "").lower()
    if broker == "bitunix":
        return s.replace("/", "")
    return s


def ccxt_swap_symbol(symbol: str) -> str:
    """CCXT erwartet für lineare Perps `BASE/QUOTE:SETTLE` (z.B. BTC/USDT:USDT).

    Ein nacktes `BTC/USDT` mit defaultType=swap führt bei vielen Börsen zu
    "does not have market symbol …" – genau das wird hier vermieden.
    """
    s = (symbol or "").strip().upper()
    if ":" in s:
        return s
    if "/" not in s:
        m = re.match(r"^([A-Z0-9]{2,10})(USDT|USDC|USD)$", s)
        if m:
            s = f"{m.group(1)}/{m.group(2)}"
    if "/" in s:
        quote = s.split("/", 1)[1]
        if quote in ("USDT", "USDC", "USD"):
            return f"{s}:{quote if quote != 'USD' else 'USDT'}"
    return s


def is_valid_symbol(symbol: str, market: str = "", *, strict: bool = True) -> bool:
    """Syntaktische Plausibilität – filtert SYNTH_*, Platzhalter und Tippfehler aus.

    `strict=False` (nur für den Synth-Broker) erlaubt auch Test-Symbole.
    """
    s = (symbol or "").strip()
    if not s:
        return False
    if strict and is_placeholder_symbol(s):
        return False
    s = s.upper()
    market = (market or "").lower()
    if market in ("crypto_perp", "perp", "futures", "swap"):
        return bool(PERP_RE.match(s.replace("/", "/")))
    if market == "crypto":
        return bool(CRYPTO_PAIR_RE.match(s)) or s in CRYPTO_BASES
    if market in ("forex",):
        return bool(re.match(r"^[A-Z]{3}/[A-Z]{3}$", s))
    # Aktien/ETF (Default)
    return bool(STOCK_RE.match(s.split(":")[0]))


def is_crypto_symbol(symbol: str) -> bool:
    s = (symbol or "").upper()
    return "/" in s or s.split("/")[0] in CRYPTO_BASES


def bar_is_partial(ts: datetime, timeframe: str, now: datetime | None = None) -> bool:
    """True wenn die Bar noch läuft (ihr Ende liegt in der Zukunft)."""
    if ts is None:
        return False
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return ts + timeframe_delta(timeframe) > now


def drop_partial_last_bar(bars: list, timeframe: str, now: datetime | None = None) -> list:
    """Offene (unfertige) Schlusskerze verwerfen – Indikatoren brauchen geschlossene Bars."""
    if not bars:
        return bars
    last = bars[-1]
    ts = getattr(last, "timestamp", None)
    if ts is not None and bar_is_partial(ts, timeframe, now):
        return bars[:-1]
    return bars


def dedupe_bars(bars: list) -> list:
    """Nach Zeitstempel sortieren und Duplikate entfernen (Ticker-Replays korrigieren)."""
    out: dict = {}
    for b in bars or []:
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        out[ts] = b
    return [out[k] for k in sorted(out.keys())]


_BROKER_SAFE = re.compile(r"^[A-Z0-9][A-Z0-9_./:\-]{1,19}$")


def is_valid_symbol_for_broker(symbol: str, market: str, broker: str) -> bool:
    """Broker-abhängige Prüfung: der Offline-Synth-Broker darf Test-Symbole."""
    s = (symbol or "").strip().upper()
    if not s:
        return False
    if str(broker or "").lower() in ("synth", "paper", "dryrun", "dry_run"):
        return bool(_BROKER_SAFE.match(s))
    return is_valid_symbol(s, market)
