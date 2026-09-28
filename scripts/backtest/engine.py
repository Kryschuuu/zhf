"""Lightweight vectorisierter Backtester – OHNE externe Frameworks
um Abhängigkeiten gering zu halten.

Replay einer historischen Signalfolge gegen OHLCV-Daten und berechnet
Kennzahlen: Win-Rate, Profit-Faktor, Sharpe, Max-Drawdown, Expectancy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from strategies.signals import compute_indicators, bars_to_df
from scripts.common.config import cfg
from scripts.common.logger import get_logger

log = get_logger("backtest")


@dataclass
class BTResult:
    symbol: str
    strategy: str
    timeframe: str
    n_trades: int
    win_rate: float
    profit_factor: float
    sharpe: float
    max_drawdown_pct: float
    expectancy_per_trade: float
    total_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    passed: bool                 # True = erfüllt Schwellen
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


# Schwellen: Nur Signale die diese Werte erreichen gehen an Risk weiter.
THRESHOLDS = {
    "min_trades": 15,            # min. Trades im Backtest-Fenster
    "min_win_rate": 0.45,        # 45%+ Winrate (wegen gutem RR ok)
    "min_profit_factor": 1.3,
    "min_sharpe": 1.0,
    "max_drawdown_pct": 15.0,
    "min_expectancy_bps": 5,     # 5 Basispunkte pro Trade (~0.05%)
}


def run_backtest(df: pd.DataFrame, signal_mask: pd.Series, direction: pd.Series,
                 sl_pct: float, tp_pct: float, fee_pct: float = 0.001) -> BTResult:
    """Vereinfachter Backtest: Signal = Entry am nächsten Open, SL/TP fest.

    signal_mask: bool-Serie (True bei Entry-Bar)
    direction:   "LONG" oder "SHORT" je Bar
    sl_pct/tp_pct: prozentualer Abstand für SL/TP (z.B. 0.015 = 1.5%)
    fee_pct:     Kommission pro Seite (0.1% bei Crypto, ~0 bei Aktien/Alpaca)
    """
    if df.empty or signal_mask.sum() == 0:
        return BTResult("-", "-", "-", 0, 0, 0, 0, 0, 0, 0, 0, 0, False, "no_trades")

    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    trades: list[float] = []  # prozentuale PnL je Trade

    entry_indices = np.where(signal_mask.to_numpy())[0]
    for ei in entry_indices:
        if ei + 1 >= n:
            continue
        entry_price = float(closes[ei])  # wir nehmen Schlusskurs als Prox für nächsten Open
        d = str(direction.iloc[ei]).upper()
        if d == "LONG":
            sl = entry_price * (1 - sl_pct)
            tp = entry_price * (1 + tp_pct)
            for j in range(ei + 1, min(ei + 200, n)):  # max 200 Bars Haltedauer
                if lows[j] <= sl:
                    trades.append(-sl_pct - 2 * fee_pct)
                    break
                if highs[j] >= tp:
                    trades.append(tp_pct - 2 * fee_pct)
                    break
            else:
                # Timeout-Exit
                trades.append((closes[min(ei + 200, n - 1)] / entry_price - 1) - 2 * fee_pct)
        else:
            sl = entry_price * (1 + sl_pct)
            tp = entry_price * (1 - tp_pct)
            for j in range(ei + 1, min(ei + 200, n)):
                if highs[j] >= sl:
                    trades.append(-sl_pct - 2 * fee_pct)
                    break
                if lows[j] <= tp:
                    trades.append(tp_pct - 2 * fee_pct)  # SHORT-Gewinn = TP-Abstand
                    break
            else:
                trades.append((entry_price / closes[min(ei + 200, n - 1)] - 1) - 2 * fee_pct)

    if not trades:
        return BTResult("-", "-", "-", 0, 0, 0, 0, 0, 0, 0, 0, 0, False, "no_trades")

    arr = np.array(trades)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    wr = len(wins) / len(arr)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(abs(losses.mean())) if len(losses) else 1e-9
    pf = float(abs(wins.sum() / losses.sum())) if losses.sum() != 0 else float("inf")
    # Sharpe (annualisiert, grob)
    if arr.std() > 0:
        sharpe = float(arr.mean() / arr.std() * np.sqrt(252))
    else:
        sharpe = 0.0
    # Max Drawdown auf Equity-Kurve
    eq = (1 + arr).cumprod()
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    mdd = float(abs(dd.min()))
    total_ret = float(eq[-1] - 1)
    expectancy = float(arr.mean())
    passed = (
        len(arr) >= THRESHOLDS["min_trades"]
        and wr >= THRESHOLDS["min_win_rate"]
        and pf >= THRESHOLDS["min_profit_factor"]
        and sharpe >= THRESHOLDS["min_sharpe"]
        and mdd * 100 <= THRESHOLDS["max_drawdown_pct"]
        and expectancy * 10000 >= THRESHOLDS["min_expectancy_bps"]
    )
    reasons = []
    if len(arr) < THRESHOLDS["min_trades"]:
        reasons.append(f"n={len(arr)}<{THRESHOLDS['min_trades']}")
    if wr < THRESHOLDS["min_win_rate"]:
        reasons.append(f"wr={wr:.2f}<{THRESHOLDS['min_win_rate']}")
    if pf < THRESHOLDS["min_profit_factor"]:
        reasons.append(f"pf={pf:.2f}<{THRESHOLDS['min_profit_factor']}")
    if sharpe < THRESHOLDS["min_sharpe"]:
        reasons.append(f"sharpe={sharpe:.2f}<{THRESHOLDS['min_sharpe']}")
    if mdd * 100 > THRESHOLDS["max_drawdown_pct"]:
        reasons.append(f"mdd={mdd*100:.1f}%>{THRESHOLDS['max_drawdown_pct']}%")

    return BTResult(
        symbol="-", strategy="-", timeframe="-",
        n_trades=len(arr), win_rate=round(wr, 3), profit_factor=round(pf, 2),
        sharpe=round(sharpe, 2), max_drawdown_pct=round(mdd * 100, 2),
        expectancy_per_trade=round(expectancy * 10000, 1),  # in Bps
        total_return_pct=round(total_ret * 100, 2),
        avg_win_pct=round(avg_win * 100, 2),
        avg_loss_pct=round(avg_loss * 100, 2),
        passed=passed,
        reason="; ".join(reasons) if reasons else "passed",
    )


def validate_candidate(candidate: dict, bars, fee_pct: float = 0.001) -> Optional[dict]:
    """Validiert ein einzelnes Signal/Strategie-Kandidat gegen historische Bars.
    Gibt den Kandidaten mit Backtest-Metriken zurück oder None wenn untauglich."""
    df = bars_to_df(bars)
    if df.empty or len(df) < 50:
        return None
    df = compute_indicators(df)

    # Rekonstruiere Signale basierend auf der Strategie
    strat = candidate.get("strategy")
    dirn = candidate.get("direction", "LONG")
    tf = candidate.get("timeframe", "1h")
    symbol = candidate.get("symbol", "")

    # Generische Signal-Maske: wir nutzen dieselben Regeln wie signals.py
    # aber für die volle Historie
    mask = pd.Series(False, index=df.index)
    direction = pd.Series("LONG", index=df.index)
    sl_pct = candidate.get("stop_loss_pct", cfg.DEFAULT_STOP_LOSS_PCT) / 100
    tp_pct = candidate.get("take_profit_pct", cfg.DEFAULT_TAKE_PROFIT_PCT) / 100

    rsi = df.get("rsi14")
    ema9 = df.get("ema9"); ema21 = df.get("ema21")
    bb_upper = df.get("bb_upper"); bb_lower = df.get("bb_lower")
    vol_ratio = df.get("volume_ratio")
    macd_hist = df.get("macd_hist")

    if strat == "mean_reversion":
        mask = rsi < 30 if dirn == "LONG" else rsi > 70
        direction = pd.Series(dirn, index=df.index)
    elif strat == "breakout":
        if dirn == "LONG":
            mask = (df["close"] > bb_upper) & (vol_ratio > 1.3)
        else:
            mask = (df["close"] < bb_lower) & (vol_ratio > 1.3)
        direction = pd.Series(dirn, index=df.index)
    elif strat == "ema_crossover":
        if ema9 is not None and ema21 is not None:
            if dirn == "LONG":
                mask = (ema9.shift(1) <= ema21.shift(1)) & (ema9 > ema21)
            else:
                mask = (ema9.shift(1) >= ema21.shift(1)) & (ema9 < ema21)
        direction = pd.Series(dirn, index=df.index)
    elif strat == "macd_flip":
        if macd_hist is not None:
            if dirn == "LONG":
                mask = (macd_hist.shift(1) < 0) & (macd_hist > 0)
            else:
                mask = (macd_hist.shift(1) > 0) & (macd_hist < 0)
        direction = pd.Series(dirn, index=df.index)
    else:
        return None

    result = run_backtest(df, mask.fillna(False), direction, sl_pct, tp_pct, fee_pct=fee_pct)
    result.symbol = symbol
    result.strategy = strat
    result.timeframe = tf

    out = dict(candidate)
    out["backtest"] = result.to_dict()
    out["validation_passed"] = result.passed
    return out


def save_result(result: dict) -> None:
    from datetime import timezone as _tz
    ts = datetime.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{result.get('symbol','NA')}_{result.get('strategy','NA')}")
    path = cfg.DATA_DIR / "backtest_results" / f"{safe}_{ts}.json"
    with path.open("w") as f:
        json.dump(result, f, indent=2, default=str)
    log.info("Backtest saved: %s (passed=%s)", path.name, result.get("validation_passed"))
