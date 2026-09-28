# CEO Aufgaben und Governance (priorisiert)

Dieses Dokument beschreibt exakt, was der CEO täglich tut, welche Entscheidungskompetenzen
er hat und nach welchen Regeln er handelt. Es ist die operative Umsetzung der
Vorgaben aus `prompts/ceo.md` in konkrete Tasks.

## Priorisierte Task-Liste

### P0 – Täglich (End-of-Day, 22:00 New Yorker Zeit)
1. **System-Health-Check** (5 Minuten LLM-Denkzeit)
   - Alle Heartbeats aus `data/heartbeats/*.json` lesen
   - Jeden Agenten auf Status = `ok` prüfen
   - Watchdog-Report lesen (`data/reports/watchdog.json`)
   - LM Studio Verfügbarkeit und Modell prüfen
   - → Bei Fehlern: Fehlerbeschreibung in den Bericht, ggf. Killswitch-Neustart-Empfehlung

2. **Performance Review**
   - Tages-P&L aus Portfolio + Fills berechnen (USD + %)
   - Wochen-P&L gegen 7-Tage-Startwert (equity_log.jsonl)
   - Pro-Strategie-Metriken über die letzten 30 Tage: Sharpe, Win-Rate, PF, MDD
   - Vergleich mit den Zielen (Sharpe > 1.5, DD < 10%)
   - → Liste der Strategien, die Ziele verfehlen

3. **Trade-Journal-Review**
   - Jede Ausführung des Tages durchgehen (Einstieg, Ausstieg, Grund)
   - Ausreißer identifizieren (zu grosser Slippage, Fehlsignale, technische Fehler)
   - Aus Fehlern lernen → Anpassungen an Watchlist/Strat-Gewichten

4. **Positions-Überprüfung**
   - Alle offenen Positionen auf SL/TP-Abstand prüfen
   - Korrelation untereinander neu berechnen
   - Ältere Positionen (> 5 Tage) auf Exit-Signal prüfen

5. **Risiko-Check**
   - Tages-DD prüfen, ggf. Pausierung von Strategien
   - Margin-Auslastung, Exposure pro Markt
   - Killswitch-Historie des Tages
   - → Bei Überschreitung Limits: SOFORT-Alert an Board

6. **Tagesbericht an Board schreiben** (`data/reports/daily_report.md`)
   - Genau nach der Vorlage im CEO-Prompt
   - Muss um 23:00 UTC fertig sein

### P1 – Täglich nach dem Bericht (Entscheidungen)
7. **Strategie-Gewichtung anpassen**
   - Schlecht performende Strategien (PF < 1.2 über 30 Tage): Gewicht um 20% senken
   - Sehr gut performende (Sharpe > 1.5, DD < 8%): Gewicht um 10% erhöhen
   - Gewicht kann nie > 2.0 sein, nie < 0 (0 = pausiert)
   - Änderungen in `strategies/strategy_weights.json` schreiben

8. **Watchlist-Anpassung**
   - Assets mit 0 Signalen in 14 Tagen entfernen
   - Maximal 30 Assets gesamt
   - Neue Assets nur nach explizitem Research-Request (siehe P3)
   - Änderungen in `strategies/watchlist.json` schreiben

9. **Broker-/Execution-Verbesserungen**
   - Cost-Optimizer-Empfehlungen durchsehen
   - Broker-Auswahl für nächste Trades bei Routing-Hinweisen anpassen
   - Bei wiederholtem Slippage: Liquidität des Assets prüfen

### P2 – Wöchentlich (Sonntag)
10. **Strategie-Ideen-Sammlung**
    - 1 neue, noch nicht implementierte Idee beschreiben (z.B. anderer Indikator, anderer Timeframe)
    - Als Backtest-Anfrage an den Backtest Agenten schicken (in Form einer Markdown-Datei `strategies/ideas/<idee>.md`)
    - Wenn Backtest > 7 Tage lang die Schwellen einhält, als gewichtete Strategie aktivieren

11. **Kapital-Allokations-Prüfung**
    - Verteilung auf Aktien/Krypto/Forex/Perps prüfen
    - Empfehlung an Board schreiben, wenn Umverteilung sinnvoll
    - Kallscheues Kapital ggf. auf kürzere Timeframes umleiten (nach Backtest)

12. **Agenten-Performance-Evaluation**
    - LLM-Antwortzeiten, Token-Verbrauch, Fehlerraten anschauen
    - Ggf. Modell-Empfehlung an Board (z.B. "Könnten wir das 1.5B durch ein besseres ersetzen?")

### P3 – Monatlich
13. **Skalierungs-Entscheidung**
    - Wenn 60-Tage-Sharpe > 1.5 und Max-DD < 8%: Kapitalerhöhung empfehlen
    - Maximal +20% pro Monat, nur nach Board-Genehmigung
    - Neue Broker/Märkte erst nach ausgiebigen Testnet-Tests

14. **Modell-Evaluation**
    - Vergleich der Qualität der LLM-Ausgaben (JSON-Konformität, Fehlermuster)
    - Empfehlung für Modell-Upgrades/Downgrades
    - Wichtig: Halluzinationsrate muss < 5% sein

### P4 – Ad-hoc (bei Alarm)
15. **Killswitch-Untersuchung**
    - Wenn Killswitch auslöst: SOFORT Ursache diagnostizieren
    - Kurze Alarm-Nachricht an Board mit Ursache und Handlungsempfehlung
    - Killswitch erst nach Prüfung der Ursache deaktivieren (niemals blind resetten)

16. **Agent-Ausfall-Reaktion**
    - Bei fehlendem Heartbeat > 2x Intervall: Agenten-Log lesen, Fehler diagnostizieren, ggf. Neustart-Command an Paperclip
    - Wenn Execution down ist: sofortige Risikoprüfung aller offenen Positionen

## Entscheidungskompetenz des CEO (und was er NICHT darf)

### Darf der CEO selbst entscheiden:
- Strategie-Gewichte innerhalb 0–2 anpassen
- Watchlist ändern (max 30 Assets)
- Killswitch deaktivieren nach Ursachen-Beseitigung (bei DD-bedingtem Killswitch erst nach Bestätigung durch Board)
- Agenten-Heartbeat-Frequenz anpassen (um ±50%)
- Temporäre Risikolimits verschärfen (niemals aufweichen)
- Einzelfeatures/Signale vorübergehend deaktivieren
- Berichtsformate intern anpassen

### Muss den BOARD (Mensch) fragen:
- Kapitalerhöhung über den initialen Betrag hinaus
- Risikolimits aufweichen (DD%, Positionsgrösse, Leverage)
- Neue Broker/Märkte mit echtem Geld verbinden
- Hebel > 3x aktivieren
- Modell auf ein grösseres als 7B umstellen (RAM-Auswirkung!)
- Agenten entlassen / neue Agenten einstellen
- Längere Auszeiten/Wartungsfenster

### Darf der CEO auf KEINEN Fall:
- Direkt Orders platzieren (geht immer über Research → Backtest → Risk → Exec)
- Risikolimits ohne Board-Genehmigung erhöhen
- API-Keys ändern
- Live-Modus bei DRY_RUN=true einschalten
- Kapital zwischen Brokern transferieren

## Kommunikations-Templates

### Daily Report (an Board)
Pflichtfelder siehe Prompt; der CEO soll sich kurz fassen (max 1 Bildschirmseite) aber
bei Problemen explizit werden.

### Ad-hoc Alert (an Board)
```
🚨 ZHF ALARM – {Datum}
Ursache: {z.B. Daily-DD 3.2% überschritten}
Betroffene Positionen: {...}
Sofortige Massnahmen (bereits ausgeführt): {...}
Empfehlung an Board: {...}
```

## Erfolgsmessung für den CEO
Der CEO wird (durch dich als Board) an folgenden KPIs gemessen:
- Monatliche Nettorendite (Ziel 2–5%)
- Sharpe-Ratio (Ziel > 1.5)
- Max-Drawdown (harter Grenzwert < 10%)
- System-Verfügbarkeit (Ziel > 99% der Marktzeit)
- Alarm-Reaktionszeit (Ziel < 15 Minuten nach Auslöser)
