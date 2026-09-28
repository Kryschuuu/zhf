---
name: daily-briefing
description: End-of-Day Analyse, Performance-Review und Tagesbericht an das Board
agent: ceo
---
# Täglicher End-of-Day Briefing

Wird um 22:00 Uhr New Yorker Zeit ausgeführt. Du musst:

1. Lesen:
   - `data/reports/fee_report.json`
   - `data/orders/state.json` + `data/logs/fills.log`
   - `data/portfolio.json`
   - `data/heartbeats/*.json` (Health aller Agenten)
   - `data/backtest_results/` der letzten 7 Tage
   - `data/reports/watchdog.json`

2. Berechnen/Analysieren:
   - Tages-P&L in USD und %
   - Wochen-Performance (Vergleich mit 7-Tage-Startwert aus equity_log.jsonl)
   - Sharpe, Win-Rate, Profit-Factor pro Strategie (über die letzten 30 Tage aus den Fills)
   - Max-Drawdown der letzten 7 Tage

3. Schreiben: `data/reports/daily_report.md` mit folgenden Abschnitten:
   - Executive Summary (2–3 Sätze)
   - Performance (Tabelle mit Zahlen)
   - Trades des Tages (jeder Trade mit Begründung + Outcome)
   - Offene Positionen (Symbol, Richtung, P&L, SL/TP-Abstand)
   - Risiko-Indikatoren (Drawdown, Korrelation, Exposure)
   - System-Health (Heartbeat-Status, Token-Verbrauch, Broker-Status)
   - Empfehlungen an den Board
   - Plan für morgen

4. Nach dem Bericht:
   - Pausiere Strategien mit PF < 1.0 der letzten 50 Trades
   - Aktualisiere `strategies/strategy_weights.json`
   - Aktualisiere `strategies/watchlist.json` (max 30 Assets)
   - Bei kritischen Problemen: füge einen expliziten "ALERT AN BOARD"-Abschnitt oben ein.

Der Bericht wird dem menschlichen Board-Operator zur Einsicht vorgelegt. Halte ihn
präzise, ehrlich und handlungsorientiert. Kein Marketing-Gerede.
