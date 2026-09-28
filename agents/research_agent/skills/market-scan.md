---
name: market-scan
description: Scannt die Watchlist über alle Broker und liefert vorselektierte Signale
agent: research
---
# Market Scan

Input: Watchlist aus `strategies/watchlist.json` (oder Default).
Vorgang:
1. Für jedes Asset: OHLCV-Daten (200 Bars) beim passenden Broker abfragen.
2. Indikatoren berechnen: RSI7/14, MACD, Bollinger, EMA9/21/50, ATR14, Volume-Ratio.
3. Regelbasierte Vorfilterung über die 5 Standard-Strategien:
   - mean_reversion: RSI<30 long, RSI>70 short
   - breakout: Schluss ausser BB + Volume > 1.3x
   - ema_crossover: EMA9 x EMA21
   - macd_flip: MACD-Histogramm Vorzeichenwechsel
4. Die vorselektierten Kandidaten (max 20) plus Feature-Matrix an das LLM schicken.
5. LLM wählt max 5 der überzeugendsten aus und justiert SL/TP.
6. Ergebnis nach `data/signals/candidates.json` schreiben.
