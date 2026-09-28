"""Risk Management Agent – läuft als Paperclip Prozess-Heartbeat.

Liest validierte Signale, Portfolio-State und Fee-Report; prüft gegen
Hard-Limits; berechnet Positionsgrössen; schreibt approved.json oder
aktiviert Killswitch.

Korrekturen:
- Killswitch wird **vor** der Arbeit geprüft (Research/Backtest/Execution taten
  das schon, Risk nicht → nach Aktivierung wurden trotzdem Orders freigegeben).
- Korrelation fragt jedes Symbole beim *eigenen* Broker ab (vorher: alle
  Positionssymbole beim Broker des Kandidaten → falsche Kurse/Fehlversuche).
  Bars werden gecacht (1 Data-Call pro Symbol statt O(n²)).
- `c['backtest']` → KeyError-Krash des ganzen Agenten, wenn der Backtest das
  Feld nicht gesetzt hat. Jetzt safe.
- Positionsgrösse: Risiko-basiert **und** gedeckelt durch verfügbares Cash und
  ein Notional-Limit; bisher wurde die Cap-Formel doppelt angewendet.
- Platzhalter-/unbekannte Symbole werden vor jedem API-Call verworfen.
- Ablehnungen landen in `data/signals/rejections.json` (statt im Nirwana).
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.common.config import cfg
from scripts.common.logger import get_logger
from scripts.common.state import SharedState, Heartbeat, now_stamp
from exchanges.factory import get_exchanges
from exchanges.symbols import is_valid_symbol_for_broker, normalize_symbol
from exchanges.base import Side, Order, OrderType

log = get_logger("risk")


class BarCache:
    """Ein Data-Call pro (broker, symbol, timeframe) pro Risk-Durchlauf."""

    def __init__(self, exchanges: dict):
        self.exchanges = exchanges
        self._cache: dict[tuple[str, str, str], list] = {}

    def closes(self, broker: str, symbol: str, tf: str) -> list[float]:
        key = (broker, symbol.upper(), tf)
        if key not in self._cache:
            ex = self.exchanges.get(broker)
            bars = ex.get_bars(symbol, tf, limit=100) if ex else []
            self._cache[key] = [float(b.close) for b in bars if getattr(b, "close", None)]
        return self._cache[key]

    def last_price(self, broker: str, symbol: str, tf: str) -> float:
        c = self.closes(broker, symbol, tf)
        return float(c[-1]) if c else 0.0


def _correlation(a_symbol: str, positions: list[dict], cache: BarCache, broker: str,
                 tf: str = "1h") -> tuple[float, str]:
    """Max |Korrelation| zwischen Kandidat und bestehenden Positionen.

    Returns (corr, partner-symbol). 0.0 heisst "nicht messbar" → nicht ablehnen,
    aber explizit markieren (Data-Grace), damit ein Feed-Ausfall nicht als
    "kein Risiko" interpretiert wird.
    """
    s_a = pd.Series(cache.closes(broker, a_symbol, tf)).pct_change().dropna()
    if len(s_a) < 30:
        return 0.0, ""
    best, partner = 0.0, ""
    for pos in positions:
        p_sym = pos.get("symbol")
        p_broker = pos.get("broker") or broker
        if not p_sym or (p_sym.upper() == a_symbol.upper() and p_broker == broker):
            continue
        s_b = pd.Series(cache.closes(p_broker, str(p_sym), tf)).pct_change().dropna()
        n = min(len(s_a), len(s_b))
        if n < 30:
            continue
        try:
            c = np.corrcoef(s_a.iloc[-n:].to_numpy(), s_b.iloc[-n:].to_numpy())[0, 1]
        except (ValueError, FloatingPointError):
            continue
        if np.isfinite(c) and abs(c) > best:
            best, partner = float(abs(c)), f"{p_sym}@{p_broker}"
    return best, partner


def _aggregate_portfolio(exchanges: dict) -> tuple[float, float, list[dict], dict]:
    total_equity = 0.0
    total_cash = 0.0
    positions: list[dict] = []
    errors: dict[str, str] = {}
    for name, ex in exchanges.items():
        try:
            acc = ex.get_account()
        except Exception as e:  # noqa: BLE001
            errors[name] = f"account: {str(e)[:120]}"
            continue
        if acc.get("error"):
            errors[name] = str(acc["error"])[:160]
        total_equity += float(acc.get("equity") or 0)
        total_cash += float(acc.get("cash") or 0)
        try:
            for p in ex.get_positions():
                positions.append({
                    "symbol": p.symbol, "qty": p.qty, "avg_entry": p.avg_entry,
                    "broker": p.broker, "unrealized_pnl": p.unrealized_pnl,
                    "market": p.market, "leverage": p.leverage,
                })
        except Exception as e:  # noqa: BLE001
            errors[f"{name}.positions"] = str(e)[:120]
    return total_equity, total_cash, positions, errors


def run() -> int:
    t0 = time.time()
    log.info("=== Risk Agent start ===")
    hb = Heartbeat(agent="risk", last_run="", status="running")
    approved_written = False
    try:
        killsw, reason = SharedState.killswitch_active()
        if killsw:
            log.warning("Killswitch aktiv (%s) – keine neuen Freigaben.", reason)
            hb.status, hb.message = "idle", f"killswitch: {reason}"
            return 0

        exchanges = get_exchanges()
        if not exchanges:
            if cfg.DRY_RUN or cfg.ENVIRONMENT == "paper":
                log.info("Keine Broker verbunden – Risk im Leerlauf (Setup-Phase).")
                hb.status, hb.message = "idle", "waiting for exchange API keys (setup)"
                return 0
            hb.status, hb.message = "error", "no exchanges"
            return 1

        total_equity, total_cash, all_positions, errors = _aggregate_portfolio(exchanges)
        SharedState.set_portfolio(total_equity, total_cash, all_positions, "risk")

        if total_equity <= 0:
            log.error("Portfolio-Equity = 0 (Broker-Fehler: %s) – Risk pausiert, "
                      "statt Positionen mit falscher Grösse freizugeben.",
                      "; ".join(f"{k}={v}" for k, v in list(errors.items())[:3]) or "unbekannt")
            hb.status = "error"
            hb.message = f"equity=0 ({list(errors.keys())[:3]})"
            hb.details = {"errors": errors}
            return 1
        if errors:
            log.warning("Portfolio teilweise erfasst – Broker-Fehler: %s", errors)

        # Circuit Breaker: Tages-/Wochen-Drawdown
        day_pnl = _pnl_pct_since(total_equity, days=1)
        if day_pnl is not None and day_pnl < -cfg.MAX_DAILY_DRAWDOWN_PCT:
            SharedState.activate_killswitch(
                f"Daily drawdown {day_pnl:.2f}% > {cfg.MAX_DAILY_DRAWDOWN_PCT}%")
            hb.status, hb.message = "error", f"daily dd {day_pnl:.2f}%"
            return 1
        week_pnl = _pnl_pct_since(total_equity, days=7)
        if week_pnl is not None and week_pnl < -cfg.MAX_WEEKLY_DRAWDOWN_PCT:
            SharedState.activate_killswitch(
                f"Weekly drawdown {week_pnl:.2f}% > {cfg.MAX_WEEKLY_DRAWDOWN_PCT}%")
            hb.status, hb.message = "error", f"weekly dd {week_pnl:.2f}%"
            return 1

        # Bereits approved? Dann nichts tun (Execution arbeitet die Liste ab)
        approved_now = SharedState.approved()
        if approved_now:
            log.info("Approved-Liste enthält bereits %d Trades – Execution arbeitet ab.",
                     len(approved_now))
            hb.status, hb.message = "ok", f"waiting for execution of {len(approved_now)} orders"
            return 0

        validated = SharedState.validated()
        if not validated:
            log.info("Keine validierten Kandidaten – nichts freizugeben.")
            hb.status, hb.message = "ok", "no validated trades"
            return 0

        if len(all_positions) >= cfg.MAX_OPEN_POSITIONS:
            log.info("Max offene Positionen erreicht (%d) – keine Freigabe.", len(all_positions))
            SharedState.set_approved([])
            approved_written = True
            hb.status, hb.message = "ok", f"max positions reached ({len(all_positions)})"
            return 0

        cache = BarCache(exchanges)
        approved: list[dict] = []
        rejected: list[dict] = []
        open_symbols = [p["symbol"] for p in all_positions]

        for c in validated:
            broker = str(c.get("broker", "alpaca")).lower()
            sym = normalize_symbol(str(c.get("symbol", "")), broker)
            market = str(c.get("market") or ("crypto_perp" if "USDT" in sym else "stocks"))
            direction = str(c.get("direction", "LONG")).upper()
            tf = str(c.get("timeframe") or "1h")
            bt = c.get("backtest") or {}
            reject = lambda reason, **kw: rejected.append({"symbol": sym, "broker": broker,
                                                            "reason": reason, **kw})  # noqa: E731

            # 1) Symbol/Broker-Plausibilität VOR jedem API-Call
            if not sym or not is_valid_symbol_for_broker(sym, market, broker):
                reject(f"invalid symbol '{c.get('symbol')}'")
                continue
            ex = exchanges.get(broker)
            if not ex:
                reject(f"broker {broker} unavailable")
                continue
            if getattr(ex, "read_only", False):
                reject(f"broker {broker} read-only (keine API-Keys / kein Testnet)")
                continue
            try:
                if not ex.is_tradable(sym):
                    reject(f"{sym} nicht handelbar bei {broker}")
                    continue
            except Exception as e:  # noqa: BLE001
                log.debug("is_tradable(%s) fehlte: %s", sym, e)
            if direction == "SHORT" and market not in ("crypto_perp", "forex"):
                reject("no short selling on stocks in first phase")
                continue

            # 2) Hard Limits
            sl_pct = float(c.get("stop_loss_pct") or cfg.DEFAULT_STOP_LOSS_PCT)
            tp_pct = float(c.get("take_profit_pct") or cfg.DEFAULT_TAKE_PROFIT_PCT)
            size_pct = float(c.get("position_size_pct_hint") or 1.0)
            sl_pct = max(0.1, sl_pct)
            if size_pct > cfg.MAX_POSITION_SIZE_PCT:
                reject(f"size {size_pct}% > max {cfg.MAX_POSITION_SIZE_PCT}%", size_pct=size_pct)
                continue
            if tp_pct / sl_pct < 2.0:
                reject(f"R:R < 1:2 (SL {sl_pct}% / TP {tp_pct}%)")
                continue
            if not bt:
                reject("kein Backtest-Ergebnis – nicht validiert")
                continue
            if bt and bt.get("passed") is False:
                reject(f"backtest failed: {bt.get('reason', 'unknown')}")
                continue

            # 3) Korrelation zu bestehenden Positionen
            corr, partner = _correlation(sym, all_positions, cache, broker, tf)
            if corr > cfg.CORRELATION_LIMIT:
                reject(f"correlation {corr:.2f} > {cfg.CORRELATION_LIMIT} mit {partner}")
                continue

            # 4) Markt offen + aktueller Preis
            if not ex.is_market_open(sym):
                reject("market closed")
                continue
            price = cache.last_price(broker, sym, tf)
            if not price or price <= 0:
                reject("no recent price (Marktdaten-Feed prüfen)")
                continue

            # 5) Positionsgrösse: Fixed-Fractional-Risiko, hart gedeckelt
            notional = total_equity * (size_pct / 100.0) / (sl_pct / 100.0)
            caps = {}
            cap_notional = total_equity * cfg.MAX_POSITION_NOTIONAL_PCT / 100.0
            leverage = min(cfg.MAX_LEVERAGE, int(c.get("leverage") or (2 if market in
                                                                        ("crypto_perp", "forex") else 1)))
            if market == "stocks":
                leverage = 1
            cap_cash = total_cash * 0.95 * leverage
            for label, cap in (("MAX_POSITION_NOTIONAL_PCT", cap_notional), ("cash", cap_cash)):
                if notional > cap:
                    caps[label] = round(notional - cap, 2)
                    notional = cap
            if notional <= 0:
                reject("kein verfügbares Kapital")
                continue
            qty = round(notional / price, 6 if market != "stocks" else 4)
            if qty * price < cfg.MIN_ORDER_NOTIONAL_USD:
                reject(f"position too small (< ${cfg.MIN_ORDER_NOTIONAL_USD:.0f})",
                       notional=round(qty * price, 4))
                continue

            sl_price = price * (1 - sl_pct / 100) if direction == "LONG" else price * (1 + sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100) if direction == "LONG" else price * (1 - tp_pct / 100)
            approved.append({
                "symbol": sym,
                "broker": broker,
                "market": market,
                "direction": direction,
                "strategy": c.get("strategy"),
                "order_type": "MARKET",
                "qty": qty,
                "leverage": leverage,
                "limit_price": None,
                "stop_loss": round(sl_price, 6),
                "take_profit": round(tp_price, 6),
                "entry_reference_price": round(price, 6),
                "notional_usd": round(qty * price, 2),
                "risk_usd": round(qty * price * sl_pct / 100, 2),
                "max_loss_pct": sl_pct,
                "confidence": c.get("confidence"),
                "correlation": round(corr, 3),
                "correlation_partner": partner,
                "sizing_caps": caps,
                "rationale": (f"n={bt.get('n_trades')}, WR={bt.get('win_rate')}, "
                              f"PF={bt.get('profit_factor')}, corr={corr:.2f}"
                              + (f", caps={list(caps)}" if caps else "")),
            })
            # Ein Trade pro Durchlauf – verhindert Überkonzentration und macht
            # die Ausführung überprüfbar (Execution holt die Liste ab).
            break

        SharedState.set_approved(approved)
        approved_written = True
        SharedState.set_risk_rejections(rejected)
        summary = defaultdict(int)
        for r in rejected:
            summary[r["reason"].split(" (")[0][:48]] += 1
        log.info("Risk: %d/%d freigegeben, %d abgelehnt%s", len(approved), len(validated),
                 len(rejected), f" ({dict(summary)})" if summary else "")
        if approved:
            log.info("Approved: %s", json.dumps(approved[0], default=str)[:400])
        hb.status = "ok"
        hb.message = (f"approved={len(approved)} rejected={len(rejected)} "
                      f"equity={total_equity:.2f} cash={total_cash:.2f}")
        hb.details = {"equity": total_equity, "cash": total_cash, "positions": len(all_positions),
                      "rejected": {k: v for k, v in summary.items()}, "broker_errors": errors}
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("Risk failed: %s", e)
        hb.status, hb.message = "error", str(e)[:200]
        return 1
    finally:
        if not approved_written and hb.status == "error":
            # Nach einem Crash lieber nichts freigeben als mit halb berechnetem State.
            SharedState.set_approved([])
        hb.last_run = now_stamp()
        hb.duration_s = time.time() - t0
        hb.write()


def _pnl_pct_since(current_equity: float, days: int = 1) -> float | None:
    """Equity-Entwicklung seit Tages-/Wochenbeginn aus data/equity_log.jsonl.

    Returns None wenn (noch) kein Referenzpunkt existiert – dann wird der
    Circuit Breaker nicht aus Unwissenheit ausgelöst, aber auch nicht übersprungen
    (None != 0.0, das ist der Unterschied zu "kein Drawdown").
    """
    p = cfg.DATA_DIR / "equity_log.jsonl"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    ref_equity = None
    lines: list[str] = []
    if p.exists():
        try:
            lines = p.read_text().splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(d.get("ts", "")) <= cutoff and "equity" in d:
                    ref_equity = float(d["equity"])
                    break
        except OSError as e:
            log.warning("equity_log nicht lesbar: %s", e)
    # aktuellen Stand anhängen (und Log bei Bedarf stutzen)
    try:
        with p.open("a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "equity": current_equity}) + "\n")
        if len(lines) > 20_000:
            keep = lines[-15_000:]
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(keep) + "\n")
            tmp.replace(p)
    except OSError as e:
        log.warning("equity_log nicht schreibbar: %s", e)
    if ref_equity and ref_equity > 0:
        return (current_equity - ref_equity) / ref_equity * 100
    return None


if __name__ == "__main__":
    sys.exit(run())
