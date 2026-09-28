"""Symbol-Validierung, Normalisierung, Bar-Bereinigung."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from exchanges.symbols import (bar_is_partial, ccxt_swap_symbol, dedupe_bars,
                               drop_partial_last_bar, is_placeholder_symbol,
                               is_valid_symbol, is_valid_symbol_for_broker,
                               normalize_symbol, to_exchange_symbol)
from exchanges.base import Bar


def test_placeholder_symbols_are_rejected():
    # Das war die Cause der "invalid symbol: SYNTH_A"-Kaskade im Risk-Agenten.
    for bad in ("SYNTH_A", "SYNTH_B", "SYNTH_ETH", "TEST1", "your_symbol", "N/A", "", "   "):
        assert not is_valid_symbol(bad, "stocks"), bad
        assert is_placeholder_symbol(bad) or not bad.strip()


def test_real_symbols_pass():
    assert is_valid_symbol("SPY", "stocks")
    assert is_valid_symbol("BRK.B", "stocks")
    assert is_valid_symbol("BTC/USD", "crypto")
    assert is_valid_symbol("BTC/USDT", "crypto_perp")
    assert is_valid_symbol("BTCUSDT", "crypto_perp")
    assert not is_valid_symbol("BTC/USDT", "stocks")
    assert not is_valid_symbol("1", "stocks")


def test_synth_broker_may_use_test_symbols():
    assert is_valid_symbol_for_broker("SYNTH_A", "stocks", "synth")
    assert not is_valid_symbol_for_broker("SYNTH_A", "stocks", "alpaca")


def test_symbol_normalisation_per_broker():
    assert normalize_symbol("btcusdt", "bitunix") == "BTC/USDT"
    assert to_exchange_symbol("BTC/USDT", "bitunix") == "BTCUSDT"
    assert ccxt_swap_symbol("BTC/USDT") == "BTC/USDT:USDT"     # ccxt-Linear-Perp
    assert ccxt_swap_symbol("BTCUSDT") == "BTC/USDT:USDT"
    assert ccxt_swap_symbol("ETH/USDC:USDC") == "ETH/USDC:USDC"


def _bar(ts: datetime, close: float = 100.0) -> Bar:
    return Bar(symbol="X", timestamp=ts, open=close, high=close, low=close, close=close, volume=1)


def test_partial_last_bar_is_dropped():
    now = datetime.now(timezone.utc)
    bars = [_bar(now - timedelta(hours=3)), _bar(now - timedelta(hours=2)),
            _bar(now - timedelta(minutes=5))]
    out = drop_partial_last_bar(bars, "1h", now=now)
    assert len(out) == 2
    assert bar_is_partial(now - timedelta(minutes=5), "1h", now)
    assert not bar_is_partial(now - timedelta(hours=2), "1h", now)


def test_drop_partial_keeps_closed_last_bar():
    now = datetime.now(timezone.utc)
    closed = [_bar(now - timedelta(hours=3)), _bar(now - timedelta(hours=2))]
    assert drop_partial_last_bar(closed, "1h", now=now) == closed


def test_dedupe_and_sort_bars():
    now = datetime.now(timezone.utc)
    b1, b2 = _bar(now - timedelta(hours=2), 1), _bar(now - timedelta(hours=1), 2)
    out = dedupe_bars([b2, b1, b2])
    assert [b.close for b in out] == [1.0, 2.0]


def test_empty_and_naive_timestamps_safe():
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    assert not bar_is_partial(old, "1h")           # naive Input darf nicht crashen
    assert drop_partial_last_bar([], "1h") == []
