"""Technical indicators & signal generation für Research Agent.

Alle Indikatoren nutzen pandas/numpy/ta und laufen auf CPU. Sie sind
einfach gehalten, damit das 3B-Modell sie interpretieren kann, aber robust
genug um als Baseline für den Backtest zu dienen.

Verfügbare Strategien (capital-effizient, für kleines Konto):
  1. mean_reversion   – RSI < 30 long, RSI > 70 short (nur per Kassa bei Aktien/Spot)
  2. breakout         – Schlusskurs über oberes Bollinger-Band = Long
  3. ema_crossover    – EMA9 kreuzt EMA21 von unten → Long
  4. rsi_divergence   – Einfache Divergenz-Erkennung (indikativ)
  5. funding_arb      – Nur Crypto: funding rate > 0.05%/8h → short spot/perp-arb-Hinweis
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import ta


@dataclass
class Signal:
    symbol: str
    broker: str           # alpaca | bingx | bitunix
    market: str           # stocks | crypto | crypto_perp | forex
    strategy: str
    direction: str        # LONG | SHORT | EXIT
    confidence: float     # 0..1
    entry_price: float
    stop_loss: float
    take_profit: float
    timeframe: str
    indicators: dict
    note: str = ""


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Berechnet Standard-Indikatoren und gibt ein DataFrame zurück."""
    df = df.copy()
    if len(df) < 30:
        return df
    if "rsi14" in df.columns and "ema21" in df.columns and "atr14" in df.columns:
        return df  # bereits berechnet (Research -> generate_signals nicht doppelt)
    df["rsi14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    df["rsi7"] = ta.momentum.RSIIndicator(df["close"], window=7).rsi()
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["ema9"] = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["atr14"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["vol_pct"] = df["close"].pct_change()
    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_ma20"]
    return df


def generate_signals(
    df: pd.DataFrame,
    symbol: str,
    broker: str,
    market: str,
    timeframe: str,
    atr_sl_mult: float = 1.5,
    atr_tp_mult: float = 3.0,
    allow_short: bool = True,
) -> list[Signal]:
    """Generiert Signale für den aktuellsten Bar."""
    if len(df) < 30:
        return []
    df = compute_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    signals: list[Signal] = []
    price = float(last["close"])
    atr = float(last.get("atr14", price * 0.01))

    def mk(direction: str, strategy: str, conf: float, note: str, sl: Optional[float] = None, tp: Optional[float] = None) -> Signal:
        if sl is None:
            sl = price - atr * atr_sl_mult if direction == "LONG" else price + atr * atr_sl_mult
        if tp is None:
            tp = price + atr * atr_tp_mult if direction == "LONG" else price - atr * atr_tp_mult
        return Signal(
            symbol=symbol, broker=broker, market=market, strategy=strategy,
            direction=direction, confidence=round(conf, 3), entry_price=price,
            stop_loss=round(float(sl), 6), take_profit=round(float(tp), 6),
            timeframe=timeframe,
            indicators={
                "rsi14": round(float(last.get("rsi14", 50)), 2),
                "rsi7": round(float(last.get("rsi7", 50)), 2),
                "macd_hist": round(float(last.get("macd_hist", 0)), 6),
                "bb_pos": "upper" if price > float(last.get("bb_upper", price)) else ("lower" if price < float(last.get("bb_lower", price)) else "mid"),
                "ema_cross": "bull" if float(last["ema9"]) > float(last["ema21"]) else "bear",
                "volume_ratio": round(float(last.get("volume_ratio", 1)), 2),
                "atr14": round(atr, 6),
            },
            note=note,
        )

    rsi = float(last.get("rsi14", 50))
    macd_hist = float(last.get("macd_hist", 0))
    prev_macd = float(prev.get("macd_hist", 0))

    # 1) Mean Reversion (RSI extremes)
    if rsi < 30:
        signals.append(mk("LONG", "mean_reversion", min(0.6 + (30 - rsi) / 100, 0.85),
                          f"RSI14={rsi:.1f} < 30, überverkauft"))
    elif rsi > 70 and allow_short:
        signals.append(mk("SHORT", "mean_reversion", min(0.6 + (rsi - 70) / 100, 0.85),
                          f"RSI14={rsi:.1f} > 70, überkauft"))

    # 2) Bollinger-Band Breakout (Schluss ausserhalb Band + Volume)
    bb_up = float(last.get("bb_upper", price))
    bb_lo = float(last.get("bb_lower", price))
    v_ratio = float(last.get("volume_ratio", 1))
    if price > bb_up and v_ratio > 1.3:
        signals.append(mk("LONG", "breakout", 0.7,
                          f"Schluss > BB upper bei Volume {v_ratio:.1f}x"))
    elif price < bb_lo and v_ratio > 1.3 and allow_short:
        signals.append(mk("SHORT", "breakout", 0.7,
                          f"Schluss < BB lower bei Volume {v_ratio:.1f}x"))

    # 3) EMA Crossover
    ema9 = float(last["ema9"]); ema21 = float(last["ema21"])
    p_ema9 = float(prev["ema9"]); p_ema21 = float(prev["ema21"])
    if p_ema9 <= p_ema21 and ema9 > ema21:
        signals.append(mk("LONG", "ema_crossover", 0.65, "EMA9 kreuzte EMA21 bullish"))
    elif p_ema9 >= p_ema21 and ema9 < ema21 and allow_short:
        signals.append(mk("SHORT", "ema_crossover", 0.65, "EMA9 kreuzte EMA21 bearish"))

    # 4) MACD-Histogramm-Wechsel
    if prev_macd < 0 and macd_hist > 0:
        signals.append(mk("LONG", "macd_flip", 0.6, "MACD-Histogramm wechselte auf positiv"))
    elif prev_macd > 0 and macd_hist < 0 and allow_short:
        signals.append(mk("SHORT", "macd_flip", 0.6, "MACD-Histogramm wechselte auf negativ"))

    return signals


def bars_to_df(bars) -> pd.DataFrame:
    """Konvertiert Liste von Bar-Objekten in ein pandas-DataFrame."""
    rows = [{"timestamp": b.timestamp, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume} for b in bars]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("timestamp").reset_index(drop=True)
    return df
