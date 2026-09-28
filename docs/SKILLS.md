# Agent-Skills für Paperclip

Paperclip-Skills sind Markdown-Dateien die Fähigkeiten eines Agenten beschreiben.
Sie können im UI hochgeladen oder direkt in das Arbeitsverzeichnis des Agenten
gelegt werden.

Jeder Skill folgt dem Paperclip-Schema:
```markdown
---
name: skill-name
description: Was der Skill tut
agent: ceo | research | ...
---
# Anweisung an den Agenten...
```

Im Folgenden die Beschreibung für die Implementierung – die Dateien sind in
diesem Repo in `agents/<role>/skills/` zur direkten Übernahme hinterlegt.

## CEO Skills
1. **daily-briefing** – EOD-Analyse und Board-Bericht
2. **strategy-allocation** – Gewichte anpassen, Watchlist ändern
3. **performance-review** – Sharpe, PF, Drawdown-Auswertung
4. **crisis-escalation** – Killswitch/Alarme an Board
5. **scaling-decision** – Kapitalerhöhung nur nach Board-Approval

## Research Skills
1. **market-scan** – Watchlist + Bars abfragen, Indikatoren berechnen
2. **pattern-identification** – 5 Strategie-Muster erkennen
3. **correlation-filter** – Doppelte Positionen vermeiden
4. **candidate-ranking** – Confidence-Scoring 0..1

## Backtest Skills
1. **historical-validation** – Replay auf OHLCV-Daten
2. **metrics-computation** – Sharpe, PF, WR, MDD, Expectancy
3. **threshold-gating** – Harte Filter gegen Overfitting
4. **result-persistence** – Protokollierung in `backtest_results/`

## Risk Skills
1. **pre-flight-check** – Killswitch, Limits, Marktstatus
2. **position-sizing** – Fixed-Fractional-Half-Kelly
3. **correlation-check** – Portfolio-Korrelationsmatrix
4. **circuit-breaker** – Drawdown/Error/Spread-Wächter

## Execution Skills
1. **order-routing** – Broker-Auswahl und Order-Erstellung
2. **bracket-orders** – SL/TP zusammen mit Entry
3. **fill-monitoring** – Polling, Idempotenz, Retry
4. **position-management** – Trailing-Stop, Time-Exit

## Cost Optimizer Skills
1. **fee-audit** – Gebühren-Aggregation pro Broker/Strategie
2. **slippage-monitor** – Ausführungsqualität
3. **broker-recommendation** – Routing-Vorschläge
4. **resource-watch** – CPU/RAM/Disk-Alarm
