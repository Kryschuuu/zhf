"""End-to-End-Selbsttest der Pipeline – ohne API-Keys, ohne Netz, ohne Risiko.

    python -m scripts.monitoring.synth_test              # kompletter Zyklus gegen den Synth-Broker
    python -m scripts.monitoring.synth_test --seed-only  # nur State schreiben (für eigene Läufe)
    python -m scripts.monitoring.synth_test --dir data/synth

Wichtig: der Test arbeitet **isoliert** in `data/synth` (ZHF_DATA_DIR) und gegen
den Fake-Broker `synth`. Früher schrieb er `SYNTH_*`-Kandidaten in den echten
State (`data/signals/validated.json`) mit `broker: "alpaca"` – der Risk-Agent
schickte diese Platzhalter dann an Alpaca (`invalid symbol: SYNTH_A`) und die
Produktion pipeline verarbeitete Testdaten. Genau das ist hier behoben.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------- setup
def _configure(data_dir: str, seed: int) -> None:
    """Env VOR den Imports setzen, damit cfg/SharedState in data/synth zeigen."""
    os.environ["ZHF_DATA_DIR"] = str(data_dir)
    os.environ["ZHF_SYNTH"] = "1"
    os.environ.setdefault("DRY_RUN", "true")
    os.environ.setdefault("LOG_LEVEL", os.getenv("LOG_LEVEL", "WARNING"))
    os.environ.setdefault("ZHF_SKIP_LLM", "1")
    os.environ["SYNTH_SEED"] = str(seed)
    random.seed(seed)


SYNTH_SYMBOLS = [
    {"symbol": "SYNTH_A", "market": "stocks", "start_price": 100.0},
    {"symbol": "SYNTH_B", "market": "stocks", "start_price": 250.0},
    {"symbol": "SYNTH_ETH", "market": "crypto", "start_price": 3000.0},
]


def _display(path: Path) -> str:
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def _gen_bars(n: int = 300, start_price: float = 100.0, vol: float = 0.015) -> list[dict]:
    """OHLCV mit Trend + Mean-Reversion – deterministisch über den Seed."""
    rnd = random.Random(f"{start_price:.4f}:{n}")
    prices = [start_price]
    for i in range(1, n):
        drift = 0.0004 * math.sin(i / 24)
        prices.append(max(0.01, prices[-1] * (1 + rnd.gauss(drift, vol))))
    ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=n)
    rows = []
    for i, c in enumerate(prices):
        o = c * (1 + rnd.gauss(0, vol * 0.3))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol * 0.5)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol * 0.5)))
        rows.append({"timestamp": ts + timedelta(hours=i), "open": o, "high": h,
                     "low": l, "close": c, "volume": float(rnd.randint(1_000_000, 5_000_000))})
    return rows


def seed_state(verbose: bool = True) -> dict:
    """Schreibt synthetische Candidates/Validated in den ISOLIERTEN State."""
    import pandas as pd

    from scripts.common.state import SharedState
    from strategies.signals import compute_indicators, generate_signals

    candidates: list[dict] = []
    validated: list[dict] = []
    for spec in SYNTH_SYMBOLS:
        df = compute_indicators(pd.DataFrame(_gen_bars(300, spec["start_price"])))
        last = df.iloc[-1]
        rsi = float(last.get("rsi14", 50) or 50)
        direction = "LONG" if rsi < 45 else ("SHORT" if rsi > 55 else "LONG")
        allow_short = spec["market"] in ("crypto_perp", "forex")
        sigs = generate_signals(df, spec["symbol"], "synth", spec["market"], "1h",
                                allow_short=allow_short) or []
        base = {
            "symbol": spec["symbol"], "broker": "synth", "market": spec["market"],
            "timeframe": "1h", "confidence": 0.66,
            "strategy": (sigs[0].strategy if sigs else "ema_crossover"),
            "direction": (sigs[0].direction if sigs else direction),
            "stop_loss_pct": 1.5, "take_profit_pct": 3.0,
            "rationale": f"Synthetic test signal – RSI={rsi:.1f}",
            "synthetic": True,
        }
        if sigs:
            base["stop_loss_pct"] = round(abs(sigs[0].entry_price - sigs[0].stop_loss)
                                          / sigs[0].entry_price * 100, 2)
            base["take_profit_pct"] = round(abs(sigs[0].take_profit - sigs[0].entry_price)
                                            / sigs[0].entry_price * 100, 2)
        candidates.append(base)
        validated.append({
            **base,
            "backtest": {"n_trades": 47, "win_rate": 0.52, "profit_factor": 1.5, "sharpe": 1.4,
                         "max_drawdown_pct": 7.5, "expectancy_per_trade": 11.0,
                         "passed": True, "reason": "synthetic ok"},
            "position_size_pct_hint": 1.0,
            "review": "Synthetisch – nur zum Pipeline-Test.",
        })

    SharedState.set_candidates(candidates)
    SharedState.set_validated(validated)
    SharedState.set_approved([])
    SharedState.set_portfolio(10_000.0, 8_500.0, [], "synth_test")
    SharedState.deactivate_killswitch()
    if verbose:
        print(f"  → {len(candidates)} synthetische Kandidaten + validierte Signale in "
              f"{os.environ['ZHF_DATA_DIR']}/signals/")
    return {"candidates": len(candidates), "validated": len(validated)}


# ------------------------------------------------------------------ pipeline
STEPS = [("research", "scripts.research.run"), ("backtest", "scripts.backtest.run"),
         ("risk", "scripts.risk.run"), ("execution", "scripts.execution.run"),
         ("cost", "scripts.cost.run"), ("watchdog", "scripts.monitoring.watchdog")]
ORDER_STEPS = [("risk", "scripts.risk.run"), ("execution", "scripts.execution.run"),
               ("cost", "scripts.cost.run")]


def run_steps(steps: list[tuple[str, str]]) -> list[tuple[str, int, float]]:
    import importlib

    results = []
    for label, module in steps:
        t0 = time.time()
        try:
            mod = importlib.import_module(module)
            rc = mod.run()
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
            results.append((label, 1, time.time() - t0))
            continue
        results.append((label, int(rc or 0), time.time() - t0))
        print(f"  [{'OK ' if not rc else 'WARN'}] {label:<10} rc={rc} ({time.time() - t0:.1f}s)")
    return results


def run_pipeline() -> list[tuple[str, int, float]]:
    return run_steps(STEPS)


def seed_order_path() -> dict:
    """Erzeugt ein validiertes Signal, das alle Risk-Gates passiert.

    Damit wird der Order-Pfad (Risk → Execution → Fills-Log → Cost-Optimizer)
    wirklich durchgespielt – der Normalpfad oben würde sonst bei
    'Backtest zu schwach, 0 Trades' stehen bleiben und genau die Bugs
    verfehlen, die in Execution/Cost sitzen (Fill-Preis 0, fehlende Gebühren).
    """
    from scripts.common.config import cfg
    from scripts.common.state import SharedState

    sym = "SYNTH_A" if cfg.SYNTH else "AAPL"
    if not cfg.SYNTH:
        sym = "AAPL"
    validated = [{
        "symbol": sym, "broker": "synth", "market": "stocks", "timeframe": "1h",
        "strategy": "ema_crossover", "direction": "LONG", "confidence": 0.8,
        "stop_loss_pct": 1.5, "take_profit_pct": 3.2, "position_size_pct_hint": 1.0,
        "backtest": {"n_trades": 60, "win_rate": 0.58, "profit_factor": 1.9, "sharpe": 2.1,
                     "max_drawdown_pct": 6.0, "expectancy_per_trade": 22.0, "passed": True,
                     "reason": "synthetic ok"},
        "review": "Synthetischer Order-Pfad-Test.",
        "synthetic": True,
    }]
    if cfg.SYNTH:
        from exchanges.factory import get_exchanges
        ex = get_exchanges().get("synth")
        if ex is not None:
            bars = ex.get_bars(sym, "1h", limit=5)
            if bars:
                validated[0]["entry_price"] = float(bars[-1].close)
    SharedState.set_validated(validated)
    SharedState.set_approved([])
    return {"validated_seeded": len(validated), "symbol": sym}


def _last_fill_row() -> dict:
    from scripts.common.fills import last_fill
    return last_fill()


def check_results() -> dict:
    from scripts.common.state import SharedState

    approved = SharedState.approved()
    order_state = SharedState.order_state()
    cands = SharedState.candidates()
    try:
        rep = json.loads(SharedState.FEE_REPORT.read_text())
    except (OSError, json.JSONDecodeError):
        rep = {}
    return {
        "candidates": len(cands),
        "validated": len(SharedState.validated()),
        "approved": len(approved),
        "open_orders": len(order_state.get("orders", [])),
        "fills": len(order_state.get("fills", [])),
        "equity": (SharedState.portfolio() or {}).get("equity"),
        "fees_usd": (rep.get("fees") or {}).get("total_fees_usd"),
        "killswitch": SharedState.killswitch_active(),
    }


def _resolve_data_dir(cli_dir: str | None) -> tuple[Path, bool]:
    """State-Verzeichnis für den Test – immer ISOLIERT.

    Präzedenz: --dir  >  ZHF_SELFTEST_DIR  >  ZHF_DATA_DIR  >  data/synth im Repo.
    (Ein DATA_DIR-Wert nur in .env verschiebt den Live-State, nicht aber den
    Selbsttest – der nutzt bewusst einen eigenen Ordner.)
    Returns (pfad, vom_test_erzeugt).
    """
    raw = (cli_dir or os.environ.get("ZHF_SELFTEST_DIR")
           or os.environ.get("ZHF_DATA_DIR") or str(ROOT / "data" / "synth"))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve(), not path.exists()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ZHF Pipeline-Selbsttest (synthetisch, isoliert)")
    ap.add_argument("--dir", default=None,
                    help="State-Verzeichnis für den Test (Standard: data/synth – "
                         "isoliert, wird vom Live-State getrennt)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seed-only", action="store_true", help="nur State schreiben, nicht rechnen")
    ap.add_argument("--keep", action="store_true",
                    help="Test-State auch dann behalten, wenn das Verzeichnis neu angelegt wurde")
    args = ap.parse_args(argv)

    data_dir, created_now = _resolve_data_dir(args.dir)
    _configure(data_dir, args.seed)

    print(f"=== ZHF Synth-Selbsttest – State: {_display(data_dir)} ===")
    stats = seed_state()
    if args.seed_only:
        print("Nur gesedet. Jetzt z.B.: "
              f"ZHF_SYNTH=1 ZHF_DATA_DIR={args.dir} python -m scripts.run_pipeline")
        return 0

    print("\n--- Pipeline ---")
    results = run_pipeline()
    summary = check_results()
    print("\n--- Order-Pfad (Risk → Execution → Cost) ---")
    seed_order_path()
    order_results = run_steps(ORDER_STEPS)
    order_summary = check_results()
    fills_row = _last_fill_row()
    print("\n--- Ergebnis ---")
    print(json.dumps({**stats, **summary, "order_path": order_summary,
                      "last_fill": fills_row}, indent=2, default=str))

    ok = all(rc == 0 for _, rc, _ in results + order_results)
    if order_summary["fills"] < 1:
        print("\n[FAIL] Order-Pfad: Execution hat keine Fills erzeugt (risk/exec defekt?).")
        ok = False
    elif not fills_row or not float(fills_row.get("avg_price") or 0) > 0 \
            or not float(fills_row.get("notional_usd") or 0) > 0:
        print("\n[FAIL] Order-Pfad: Fill ohne Preis/Notional – Cost-Optimizer wäre blind.")
        ok = False
    if summary["killswitch"][0]:
        print(f"\n[FAIL] Killswitch aktiv: {summary['killswitch'][1]}")
        ok = False
    print("\n" + ("[OK] Selbsttest bestanden – Pipeline läuft Ende-zu-Ende."
                  if ok else "[WARN] Selbsttest mit Befunden – Ausgaben prüfen."))
    # Nur ein Verzeichnis, das WIRKlich neu angelegt wurde, räumen wir auf –
    # nie den State eines vorhandenen Laufwerks.
    if created_now and not args.keep:
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
        print(f"(Test-State in {_display(data_dir)} wurde wieder entfernt – --keep behält ihn)")
    else:
        print(f"(State liegt in {_display(data_dir)} – Absicht: nachvollziehbar bleiben)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
