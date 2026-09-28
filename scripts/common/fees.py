"""Zentrale Gebühren-Referenz (Taker/Maker) für Backtest, Cost-Optimizer und Risk.

Warum zentral? Der Backtest rechnete pauschal mit 0.1 % pro Seite für alles ausser
Alpaca-Aktien, der Cost-Optimizer mit 0.1 % pro Trade – und beide lagen damit für
Krypto-Perps und Alpaca-Spot falsch. Das killt bzw. beschönigt die
Rentabilitätsrechnung. Zahlen Stand 2026 (VIP0, Taker für Market-Orders):

| Broker  | Markt        | Maker  | Taker  |
|---------|--------------|--------|--------|
| alpaca  | stocks/etf   | 0.00 % | 0.00 % |  (Commission-free, Regulierungsgebühren ≈ 0)
| alpaca  | crypto spot  | 0.15 % | 0.25 % |
| bingx   | perp         | 0.02 % | 0.05 % |
| bitunix | perp         | 0.02 % | 0.06 % |

Overrides per `.env`: `FEE_SCHEDULE_JSON='{"bingx":{"crypto_perp":0.0004}}'`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

# (broker, market) -> (maker, taker) in Prozent des Notionals, pro Seite
DEFAULT_SCHEDULE: dict[str, dict[str, tuple[float, float]]] = {
    "alpaca": {"stocks": (0.0, 0.0), "etf": (0.0, 0.0), "crypto": (0.0015, 0.0025),
               "forex": (0.0, 0.0002)},
    "bingx": {"crypto_perp": (0.0002, 0.0005), "crypto": (0.001, 0.001)},
    "bitunix": {"crypto_perp": (0.0002, 0.0006), "crypto": (0.0008, 0.001)},
    "synth": {"stocks": (0.0, 0.0), "crypto": (0.001, 0.001), "crypto_perp": (0.0002, 0.0005)},
}
DEFAULT_PAIR = (0.001, 0.001)

# Funding Perpetuals: 0.01 % pro 8h ist ein typischer Richtwert (stark schwankend).
DEFAULT_FUNDING_PER_8H = 0.0001


def _load_schedule() -> dict:
    sched = {k: dict(v) for k, v in DEFAULT_SCHEDULE.items()}
    raw = os.getenv("FEE_SCHEDULE_JSON", "").strip()
    if raw:
        try:
            for broker, markets in json.loads(raw).items():
                entry = sched.setdefault(str(broker).lower(), {})
                for market, rate in (markets or {}).items():
                    if isinstance(rate, (int, float)):
                        entry[str(market).lower()] = (float(rate), float(rate))
                    elif isinstance(rate, (list, tuple)) and len(rate) == 2:
                        entry[str(market).lower()] = (float(rate[0]), float(rate[1]))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            import logging
            logging.getLogger("zhf.fees").warning("FEE_SCHEDULE_JSON ungültig (%s) – Defaults", e)
    return sched


SCHEDULE = _load_schedule()


@dataclass(frozen=True)
class FeeInfo:
    broker: str
    market: str
    maker: float
    taker: float

    @property
    def round_trip_taker(self) -> float:
        return self.taker * 2

    def as_dict(self) -> dict:
        return {"broker": self.broker, "market": self.market, "maker": self.maker,
                "taker": self.taker, "round_trip_taker": round(self.round_trip_taker, 6)}


def fees(broker: str, market: str) -> FeeInfo:
    b = str(broker or "").lower()
    m = str(market or "").lower()
    maker, taker = SCHEDULE.get(b, {}).get(m, DEFAULT_PAIR)
    return FeeInfo(b, m, float(maker), float(taker))


def taker_fee(broker: str, market: str) -> float:
    return fees(broker, market).taker


def maker_fee(broker: str, market: str) -> float:
    return fees(broker, market).maker


def round_trip_fee(broker: str, market: str) -> float:
    """Pro Trade (Entry + Exit) in Prozent des Notionals – konservativ: nur Taker."""
    return fees(broker, market).round_trip_taker


def expected_costs(notional: float, broker: str, market: str, *, hold_hours: float = 4.0,
                   entry_is_maker: bool = False, exit_is_maker: bool = False) -> dict:
    """Schätzung aller Handelskosten inkl. Funding bei Perps."""
    info = fees(broker, market)
    entry = info.maker if entry_is_maker else info.taker
    exit_ = info.maker if exit_is_maker else info.taker
    commission = notional * (entry + exit_)
    funding = 0.0
    if "perp" in (market or ""):
        funding = abs(notional) * DEFAULT_FUNDING_PER_8H * (max(0.0, hold_hours) / 8.0) * 2
    total = commission + funding
    return {
        "notional": round(notional, 2),
        "commission_usd": round(commission, 4),
        "funding_usd": round(funding, 4),
        "total_usd": round(total, 4),
        "total_pct": round((total / notional * 100) if notional else 0.0, 4),
        "fees": info.as_dict(),
    }


def breakeven_move_pct(broker: str, market: str) -> float:
    """Preisbewegung in % des Kurses, die nötig ist um Entry+Exit zu decken.

    Die Hebelwirkung kürzt den Break-even *nicht*: Gebühren fallen auf dem
    vollen Notional an, und genau dieser Notional bewegt sich um x %.
    """
    return round(round_trip_fee(broker, market) * 100, 4)


def min_sane_take_profit_pct(broker: str, market: str, multiple: float = 4.0) -> float:
    """TP sollte deutlich über den Round-Trip-Kosten liegen (Default: 4x)."""
    return round(breakeven_move_pct(broker, market) * max(1.0, multiple), 4)
