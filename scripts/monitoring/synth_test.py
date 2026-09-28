"""Generate synthetic market data for end-to-end pipeline testing.

Usage: python -m scripts.monitoring.synth_test

Creates fake candles + signals so you can validate the pipeline without
API keys or LM Studio. Useful after initial setup to verify all JSON
files are correctly written and consumed.
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.state import SharedState
from scripts.backtest.engine import run_backtest, compute_indicators  # type: ignore
from strategies.signals import compute_indicators as strat_compute

SEED = 42
random.seed(SEED)
np.random.seed(SEED)


def _gen_bars(n: int = 300, start_price: float = 100.0, vol: float = 0.015):
    """Erzeugt einfache OHLCV-Bars mit etwas Trending + Mean-Reversion."""
    prices = [start_price]
    for i in range(1, n):
        # leicht mean-reverting noise + drift
        drift = 0.0002 * math.sin(i / 30)
        ret = random.gauss(drift, vol)
        prices.append(max(0.01, prices[-1] * (1 + ret)))
    rows = []
    ts = datetime.now(timezone.utc) - timedelta(hours=n)
    for i, c in enumerate(prices):
        o = c * (1 + random.gauss(0, vol * 0.3))
        h = max(o, c) * (1 + abs(random.gauss(0, vol * 0.5)))
        l = min(o, c) * (1 - abs(random.gauss(0, vol * 0.5)))
        v = random.randint(1_000_000, 5_000_000)
        rows.append({
            "timestamp": ts + timedelta(hours=i),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    df = pd.DataFrame(rows)
    return df


def run_demo():
    print("Generating synthetic test data...")
    symbols = [
        ("SYNTH_A", 100.0), ("SYNTH_B", 250.0), ("SYNTH_ETH", 3000.0),
    ]

    candidates = []
    for sym, p0 in symbols:
        df = _gen_bars(300, p0)
        df = strat_compute(df)
        last = df.iloc[-1]
        rsi = float(last.get("rsi14", 50))
        direction = "LONG" if rsi < 45 else ("SHORT" if rsi > 55 else "LONG")
        candidates.append({
            "symbol": sym, "broker": "alpaca", "market": "stocks",
            "strategy": "ema_crossover" if random.random() > 0.5 else "mean_reversion",
            "direction": direction, "confidence": round(0.55 + random.random() * 0.25, 2),
            "timeframe": "1h",
            "stop_loss_pct": 1.5, "take_profit_pct": 3.0,
            "rationale": f"Synthetic test signal – RSI={rsi:.1f}",
        })

    SharedState.set_candidates(candidates)
    print(f"  → wrote {len(candidates)} synthetic candidates")

    # Simpler fake Backtest: alle als bestanden markieren
    validated = []
    for c in candidates:
        validated.append({
            **c,
            "backtest": {
                "n_trades": 47, "win_rate": 0.52, "profit_factor": 1.5,
                "sharpe": 1.4, "max_drawdown_pct": 7.5,
                "expectancy_per_trade": 11.0, "passed": True, "reason": "synthetic ok"
            },
            "position_size_pct_hint": 1.0,
            "review": "Synthetisch – nur zum Pipeline-Test.",
        })
    SharedState.set_validated(validated)
    print(f"  → wrote {len(validated)} validated trades")

    # Simples Portfolio
    SharedState.set_portfolio(eq=10000.0, cash=8500.0, positions=[], source="synth_test")

    # Approved-Liste für Execution (die wird sonst von Risk erzeugt)
    # Für den Demo-Test lassen wir sie leer oder fügen ein Dry-Run-Pending hinzu.
    SharedState.set_approved([])

    # Killswitch zurücksetzen
    SharedState.deactivate_killswitch()
    print("\nSynthetic state written. You can now run:")
    print("  python -m scripts.risk.run")
    print("  python -m scripts.execution.run")
    print("  python -m scripts.cost.run")
    print("to verify each step.")


if __name__ == "__main__":
    run_demo()
