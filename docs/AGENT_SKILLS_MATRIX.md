# Agent Skills-Matrix – Inputs / Outputs / Interaktionen

Jede Zelle beschreibt exakt, was der Agent tut, welche Daten er nutzt, was er
produziert und mit wem er kommuniziert.

---

## Modell-Übersicht (gilt für alle LLM-Agenten)
**Primär: OpenCode Zen-Free-Modelle (keine API-Keys, keine Kosten):**

| Free-Modell | Vendor | Context | Einsatz bei |
|-------------|--------|---------|-------------|
| `big-pickle` | (Mystery/Coding) | 200k | Research, Coding-Tasks |
| `space-bunny-free` | (Zero-retention) | 16k | Privacy-sensitive Aufgaben |
| `longcat-2.5-preview-free` | (LongCat) | 200k | CEO-Monatsberichte, lange Analysen |
| `mimo-v2.5-flash-free` | Xiaomi | 16k | Cost Optimizer (schnell) |
| `mimo-v2-pro-free` | Xiaomi | 32k | Reserve für Research |
| `minimax-m2.5-free` | MiniMax | 200k | Risk/CRO (zuverlässiges JSON) |
| `gpt-5-nano` | OpenAI | 8k | Backtest-Review (schnell, klein) |
| `nemotron-3-super-free` | NVIDIA | 200k | Reserve CEO/Risk (120B) |
| `neotone-3-ultra-free` / `nemotron-3-ultra-free` | NVIDIA | 204k | **CEO** (550B MoE, stärkstes Free-Modell) |
| `nemotron-3.5-lightning-free` | NVIDIA | 16k | Einfache Tasks |

**Fallback: LM Studio lokal** (Q4_K_M, nur bei Internet-/OpenCode-Ausfall):
- `qwen2.5-7b-instruct-q4_k_m` → CEO
- `llama-3.2-3b-instruct-q4_k_m` → Research/Risk
- `qwen2.5-1.5b-instruct-q4_k_m` → Cost/Backtest-Review

---

## 1. CEO (Chief Executive Officer) – Elara Voss

| Bereich | Beschreibung |
|---------|--------------|
| **Rolle** | Strategische Leitung, Kapitalallokation, Performance-Monitoring, Board-Kommunikation |
| **Primäres Modell** | `opencode/nemotron-3-ultra-free` (Neotone/Nemotron 3 Ultra Free, 550B MoE) |
| **Alternativen** | `opencode/longcat-2.5-preview-free`, `opencode/nemotron-3-super-free` |
| **Offline-Fallback** | LM Studio Qwen 2.5 7B |
| **Adapter** | opencode_local |
| **Heartbeat** | Täglich EOD 22:00 NY-Z (04:00/05:00 CET) |
| **Inputs** | `data/portfolio.json`, `data/orders/state.json`, `data/logs/fills.log`, `data/reports/fee_report.json`, `data/reports/watchdog.json`, `data/equity_log.jsonl`, `data/heartbeats/*.json`, `data/backtest_results/*.json` |
| **Outputs** | `data/reports/daily_report.md`, `strategies/strategy_weights.json`, `strategies/watchlist.json`, Alerts an Board |
| **Empfängt von** | Cost Optimizer, Execution, Risk (Killswitch), Watchdog |
| **Sendet an** | Research (Watchlist + Weights), Backtest (Ideen), Cost (Ziele), Board (Berichte) |

## 2. Research Agent – Jordan Chen

| Bereich | Beschreibung |
|---------|--------------|
| **Rolle** | Marktscan, Mustererkennung, Handelsideen |
| **Primäres Modell** | `opencode/big-pickle` |
| **Alternativen** | `opencode/mimo-v2-pro-free`, `opencode/minimax-m2.5-free` |
| **Offline-Fallback** | LM Studio Llama 3.2 3B |
| **Adapter** | opencode_local |
| **Heartbeat** | Alle 30 Minuten |
| **Inputs** | `strategies/watchlist.json`, OHLCV 200 Bars von Brokern, `data/portfolio.json`, `strategies/strategy_weights.json` |
| **Datenquellen** | Alpaca Market Data API, BingX/CCXT OHLCV, Bitunix OHLCV |
| **Outputs** | `data/signals/candidates.json` (max 5 Kandidaten) |
| **Empfängt von** | CEO (Watchlist, Weights) |
| **Sendet an** | Backtest Agent |
| **Skills** | market-scan, pattern-identification (5 Strategien), correlation-filter, candidate-ranking |
| **Indikatoren** | RSI(7,14), MACD+Histogramm, Bollinger(20,2), EMA(9,21,50), ATR(14), Volume-Ratio |

## 3. Backtest Agent – Priya Patel

| Bereich | Beschreibung |
|---------|--------------|
| **Rolle** | Historische Validierung von Signalen, Overfitting-Schutz |
| **Primäres Modell** | **KEINES** für den Kern – rein Python/pandas/numpy. Kurze Plausibilitäts-Review via `opencode/gpt-5-nano`. |
| **Alternativen** | `opencode/mimo-v2.5-flash-free` |
| **Adapter** | process |
| **Heartbeat** | Alle 15 Minuten |
| **Inputs** | `data/signals/candidates.json`, OHLCV 300 Bars pro Kandidat |
| **Outputs** | `data/signals/validated.json`, Einzelresultate in `data/backtest_results/*.json` |
| **Empfängt von** | Research Agent |
| **Sendet an** | Risk Agent |
| **Skills** | historical-validation, metrics-computation (Sharpe/PF/WR/MDD/Expectancy), threshold-gating, result-persistence |
| **Filter** | min 15 Trades, WR ≥ 45%, PF ≥ 1.3, Sharpe ≥ 1.0, DD ≤ 15%, Expectancy ≥ 5 Bps |

## 4. Risk Management Agent – Marcus Okonkwo (CRO)

| Bereich | Beschreibung |
|---------|--------------|
| **Rolle** | Letztes Wort vor jedem Trade, Kapitalschutz, Limits durchsetzen |
| **Primäres Modell** | `opencode/minimax-m2.5-free` (zuverlässiges JSON, lange Kontexte) |
| **Alternativen** | `opencode/big-pickle`, `opencode/nemotron-3-super-free` |
| **Offline-Fallback** | LM Studio Llama 3.2 3B (via subprocess über llm.py) |
| **Adapter** | process (ruft intern opencode CLI auf) |
| **Heartbeat** | Alle 5 Minuten |
| **Inputs** | `data/signals/validated.json`, `data/portfolio.json`, `data/orders/state.json`, `data/reports/fee_report.json` |
| **Outputs** | `data/orders/approved.json`, ggf. `data/killswitch.json` |
| **Empfängt von** | Backtest Agent |
| **Sendet an** | Execution Agent, CEO (Alarm) |
| **Skills** | pre-flight-check, position-sizing (Half-Kelly), correlation-check, circuit-breaker |
| **Hard Limits** | 2% Positionsgrösse, 3% Tages-DD, 7% Wochen-DD, max 8 Positionen, max 3x Hebel, SL-Pflicht, R:R ≥ 1:2, Korrelation ≤ 0.7 |

## 5. Execution Agent – Sasha Kowalski

| Bereich | Beschreibung |
|---------|--------------|
| **Rolle** | Order-Routing, Fills überwachen, Positionen managen |
| **Modell** | **KEINES** – deterministische Python-Logik (Halluzinationsschutz!) |
| **Adapter** | process |
| **Heartbeat** | Jede Minute während Marktzeiten |
| **Inputs** | `data/orders/approved.json`, `data/killswitch.json`, Broker-APIs |
| **Outputs** | `data/orders/state.json`, `data/logs/fills.log`, aktualisiert `data/portfolio.json` |
| **Empfängt von** | Risk Agent |
| **Sendet an** | Cost Optimizer (Fills), CEO (Status), Broker (Orders) |
| **Skills** | order-routing, bracket-orders, fill-monitoring, position-management, idempotent-retry |
| **Broker** | Alpaca (Stocks/Crypto), BingX (CCXT), Bitunix (eigene API) |

## 6. Cost Optimizer – Naomi Bergström

| Bereich | Beschreibung |
|---------|--------------|
| **Rolle** | Gebühren/Slippage minimieren, System-Ressourcen überwachen |
| **Primäres Modell** | `opencode/mimo-v2.5-flash-free` (schnelles Xiaomi-Modell) |
| **Alternativen** | `opencode/gpt-5-nano`, `opencode/space-bunny-free` (wenn Privacy) |
| **Offline-Fallback** | LM Studio Qwen 2.5 1.5B |
| **Adapter** | process (ruft intern opencode CLI auf) |
| **Heartbeat** | Stündlich |
| **Inputs** | `data/logs/fills.log`, `data/orders/state.json`, Broker-Fees, psutil (CPU/RAM/Disk) |
| **Outputs** | `data/reports/fee_report.json` |
| **Empfängt von** | Execution (Fills) |
| **Sendet an** | CEO, Risk, Execution |
| **Skills** | fee-audit, slippage-monitor, broker-recommendation, resource-watch |
| **Alarmschwellen** | Fees > 0.15%/Trade, Slippage > 0.3% (Aktien) / 0.5% (Crypto), RAM > 85%, Disk > 80% |

---

## Kommunikationsmatrix

```
            ┌─────── BOARD (Mensch) ───────┐
            │     ▲ Alerts/Tagesberichte    │
            │     │                        │
            │    CEO ──────────────────────┘
            │     │  Nemotron-3-Ultra-Free
            ├─────┼─────────────────────────── Free-Tier
            │     │                            über OpenCode
            │  Cost (MiMo-Flash)             CLI
            │   ▲                              │
            │   │                              ▼
            │   │                         Research (Big Pickle)
            │   │                              │
            │   │                              ▼
            │   └──── Risk (MiniMax) ←── Backtest (numerisch + GPT-5-Nano Review)
            │            ▲                  │
            │            │                  ▼
            │            └──── Execution (KEIN LLM)
            │                              │
            └──────────────────────────────┘
                                           ▼
                              Broker (Alpaca/BingX/Bitunix)
```

## Gemeinsamer Zustand (Dateisystem, atomic writes)
- `data/signals/candidates.json` – Research → Backtest
- `data/signals/validated.json` – Backtest → Risk
- `data/orders/approved.json` – Risk → Execution
- `data/orders/state.json` – Execution → alle
- `data/portfolio.json` – Execution → alle
- `data/killswitch.json` – Risk → alle (globales STOP-Signal)
- `data/reports/fee_report.json` – Cost → CEO/Risk
- `data/reports/daily_report.md` – CEO → Board
- `data/heartbeats/*.json` – jeder Agent → Watchdog

Paperclip selbst zeichnet alle Agent-Runs in seiner PostgreSQL-DB auf
(Audit-Log im Dashboard). Die JSON-Dateien bilden den Laufzeit-Zustand ab.
