# Fehlersuche (Troubleshooting) – ZHF 0.2.1

Alle Punkte sind mit einem Befehl reproduzierbar. Die Pipeline läuft offline –
**nirgends** wird still auf `0`, `[]` oder „keine Chance“ zurückgefallen: ein
Feeder-Fehler landet im Statusreport (`data/market_data/status.json`) und im
Watchdog-Befund, nicht als gezeichnete Order.

## 1. Befund → Ursache → Fix

| Log-Zeile / Symptom | Ursache | Fix (umgesetzt) | Prüfen mit |
|---|---|---|---|
| Alpaca `42210000 subscription does not permit querying recent SIP data` | Free-Plan darf kein explizites `feed=sip` für Daten < 15 min | `ALPACA_DATA_FEED=auto` ⇒ pro Request `feed=DataFeed.IEX` (Client-Konstruktor kennt kein `feed` mehr), `end` **timezone-bewahrt** | `python -m scripts.monitoring.synth_test`, `pytest tests/test_alpaca.py` |
| Dieselbe Meldung, obwohl `iex` gesetzt | Uhrzeit-Drift: `end` in die Zukunft gerechnet | Drift-Warnung im Adapter (> 120 s → Warnung, `system clock skew`) | `watchdog`-Report, Log-Zeile `Clock drift` |
| `no bars for X` / Risk `0/3 approved` ohne Grund | Datenlage und Abwesenheit von Chancen vermischt | `data_status` pro Asset (`ok`/`empty`/`error`/`stale`), Risk meldet `data_insufficient` explizit, Watchdog wird `critical` | `cat data/market_data/status.json` |
| `fapi-sim.bitunix.com` DNS-Fehler | Dieser Host existiert nicht – Bitunix hat **kein** öffentliches Futures-REST-Testnet | Base-URL `https://fapi.bitunix.com` aus `BITUNIX_BASE_URL`; `BITUNIX_TESTNET=true` ⇒ **read-only**, Orders geblockt (`BITUNIX_ALLOW_LIVE_ORDERS=true` zum Explizitmachen) | `pytest tests/test_bitunix.py` |
| `SYNTH_A … invalid symbol` bei Alpaca | Der Selbsttest schrieb Test-Kandidaten in den echten State (`broker: "alpaca"`) | Selbsttest lebt in `data/synth` (`ZHF_DATA_DIR`), Broker `synth`; Fabrikgate `SYNTH_A@alpaca → None`; Symbol-Hygiene in `exchanges/symbols.py` | `pytest tests/test_pipeline_integration.py` |
| Alpaca-Order „hing“ 300 s / Fill-Preis 0 | Bracket-Auftragsbeine (SL/TP) wurden in **offenen** Orders gesucht | `get_order(order_id)` pro Kind-Order, Fallback auf Parent-Snapshot; Dry-Run-Fill bekommt Referenzpreis + Notional | `pytest tests/test_execution.py` |
| „zu wenig Cash“ trotz Paper-Konto | Qty aus `PRICE × MAX_POSITION_SIZE_PCT` statt Notional | Notional-Basiert (`MAX_POSITION_NOTIONAL_PCT`), `<1` Aktie → `notional`-Order, unter `MIN_ORDER_NOTIONAL_USD` ⇒ klarer Reject (`below_broker_min`) | `pytest tests/test_execution.py` |
| Backtest lehnt alles ab („LLM fehlt“) | Regelwerk-Review war an das LLM gekoppelt | `_rule_review()` (harte Regeln), `_merge_reviews()` mit Veto-Floor: ohne LLM ⇒ neutrale 5 statt Reject | `pytest tests/test_research.py` |
| Perps/Forex ohne jedes Signal | `MarketPhase.CLOSE` für 24/7-Märkte (Zeitfenster-Filter) | `_market_phase()` liefert für `crypto*`/`forex` = 24/7 offen | `pytest tests/test_research.py` |
| Research ignoriert verwaiste Kandidaten | Candidate ohne Broker wurde still verworfen | `_requeue_missing_brokers()` (max. 3 Versuche, `pending_brokers`) | `pytest tests/test_research.py` |
| Ein Broker legt die ganze Pipeline lahm | Kein Retry/Breaker, Fehler als Payload statt Exception | `scripts/common/net.py`: `retry_call` mit Backoff/`Retry-After`, Circuit-Breaker je Broker (Half-Open-Probe), `note()`, `health_snapshot()` | `python -m scripts.monitoring.watchdog` |

## 2. Gebühren & Break-even

`scripts/common/fees.py` ist die **einzige** Quelle für Gebühren (Research-Filter,
Risk-Gate, Execution-Tracking, Cost-Optimizer):

| Broker / Markt | Maker | Taker | Round-Trip | Mindest-Take-Profit |
|---|---|---|---|---|
| alpaca stocks/ETF | 0 | 0 | 0 | 0,15 % (Puffer) |
| alpaca crypto | 0,15 % | 0,25 % | 0,40 % | 0,60 % |
| bingx crypto_perp | 0,02 % | 0,05 % | 0,10 % | 0,30 % |
| bitunix crypto_perp | 0,02 % | 0,06 % | 0,12 % | 0,32 % |

Wichtig: **Der Hebel senkt den Break-even nicht** – Gebühren laufen auf dem vollen
Notional. `min_sane_take_profit_pct()` (Faktor `RISK_MIN_TP_MULTIPLE`, Default 2)
verhindert Limit-Orders, deren Gewinn die Round-Trip-Kosten nicht deckt; ohne dieses
Gate wären 90 % der Signale statistisch Verlust. Überschreiben mit `FEE_SCHEDULE_JSON`
(JSON: `{"broker": {"markt": [maker, taker]}}`).

## 3. Datenisolierung (Test vs. Live)

```bash
python -m scripts.run_pipeline --synth        # nutzt data/synth, Broker "synth"
python -m scripts.monitoring.synth_test       # dito, räumt frisch erzeugte Dirs auf
python -m scripts.monitoring.synth_test --keep --dir /tmp/zhf-test
ZHF_DATA_DIR=/tmp/zhf-live-test python -m scripts.run_pipeline --only research,risk
```

* `--dir` > `ZHF_DATA_DIR` > `data/synth`. Ein `DATA_DIR` **nur** in `.env` verschiebt
  den Live-State, nicht den Selbsttest (dafür ist `SELFTEST_DIR`/`--dir` da).
* `DRY_RUN=true` ist Default. Echte Orders brauchen `DRY_RUN=false` **und**
  `BITUNIX_ALLOW_LIVE_ORDERS=true` (Bitunix) respektive gefüllte Keys.

## 4. Wenn etwas klemmt – Reihenfolge

1. `python -m scripts.monitoring.watchdog` → `data/reports/watchdog.json`
   (Marktdaten, Broker-Breaker, Killswitch, Ressourcen, LLM-Status).
2. `cat data/market_data/status.json` → welche Assets haben `empty`/`error` und warum.
3. `cat data/signals/rejections.json` → jede Ablehnung mit `reason` (Research/Risk).
4. `cat data/orders/recent_errors.json` → Order-Fehler nach Kategorie
   (`symbol_invalid`, `rate_limited`, `dns`, `not_supported`, `no_order`), `killswitch_hint`.
5. `tail -40 data/logs/fills.log` → Fills inkl. `ref_price`, `slippage_bps`, `est_fee_usd`, `mode`.
6. Selbsttest: `python -m scripts.monitoring.synth_test` – läuft ohne Keys und ohne Netz.

**Killswitch-Politik (0.2.1):** kritische Watchdog-Befunde lösen den Killswitch nur aus,
wenn **mindestens ein Broker verbunden** ist – sonst blockiert ein frisches Setup (keine
Keys, kein LLM) jeden weiteren Lauf, ohne dass etwas zu schützen wäre. Ohne Broker bleibt
es bei `CRITICAL` im Report und Exit-Code 1. Zurücksetzen nach behobener Ursache:

```bash
python -m scripts.monitoring.watchdog --reset-killswitch   # schreibt active=false (Audit-Trail bleibt)
```

## 5. Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest            # 100 Tests, komplett offline (Stubs, Fake-HTTP, gefälschte Zeit)
python -m pytest -m integration -q   # nur die End-to-End-Läufe
```

Kein Test berührt `data/signals` im Repo – `tests/conftest.py` setzt `ZHF_DATA_DIR`
**vor** dem Import von `config` auf ein Temp-Verzeichnis.

## 6. Bekannte Grenzen (absichtlich so)

* **Bitunix**: kein öffentliches Futures-REST-Testnet → nur Read-only-Modus; Live-Orders
  sind ein bewusster, expliziter Schalter.
* **Alpaca Free-Tier**: IEX statt SIP (anderer Marktdaten-Mix), 200 Requests/min,
  keine Shorting-Breakout-Signale ohne `extended_hours`.
* **Backtest** ist ein Regelfilter, keine leere Erwartung: 0 Trades bedeuten „nicht
  bestanden“, nicht „Daten fehlen“ – die Unterscheidung steht in `rejections.json`.
* **Kosten** im Dry-Run: Fees/Slippage werden aus Bar-Spread + `fees.py` geschätzt;
  die Abweichung zum Broker-Fill wird im Cost-Optimizer als `slippage_bps` gemeldet.
