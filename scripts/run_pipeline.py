"""Run one full pipeline cycle: research → backtest → risk → execution → cost.

Nützlich für manuelle Tests und Initial-Setup. Im Betrieb wird jeder Schritt
einzeln per Paperclip-Heartbeat getriggert.

Usage:
  .venv/bin/python -m scripts.run_pipeline
  .venv/bin/python -m scripts.run_pipeline --synth        # Offline-Selbsttest (data/synth)
  .venv/bin/python -m scripts.run_pipeline --only risk,execution
  .venv/bin/python -m scripts.run_pipeline --fail-fast    # Abbruch bei erstem Fehler

Fixes: der Interpreter war hart auf `.venv/bin/python` gesetzt – ohne venv
brach der Lauf mit `FileNotFoundError` ab. Jetzt Fallback auf das aktuelle
Python, optional `--python <pfad>`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEPS = [
    ("research",  ["-m", "scripts.research.run"]),
    ("backtest",  ["-m", "scripts.backtest.run"]),
    ("risk",      ["-m", "scripts.risk.run"]),
    ("execution", ["-m", "scripts.execution.run"]),
    ("cost",      ["-m", "scripts.cost.run"]),
    ("watchdog",  ["-m", "scripts.monitoring.watchdog"]),
]


def _python(explicit: str | None) -> str:
    if explicit:
        return explicit
    venv = ROOT / ".venv" / ("bin/python" if os.name != "nt" else "Scripts/python.exe")
    return str(venv) if venv.exists() else sys.executable


def _heartbeat_summary(data_dir: Path) -> dict:
    out = {}
    for hb_file in sorted((data_dir / "heartbeats").glob("*.json")):
        try:
            d = json.loads(hb_file.read_text())
            out[hb_file.stem] = {"status": d.get("status"), "message": (d.get("message") or "")[:110]}
        except (OSError, json.JSONDecodeError):
            out[hb_file.stem] = {"status": "unreadable"}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ZHF Pipeline – ein kompletter Zyklus")
    ap.add_argument("--only", default="", help="Kommaliste: research,backtest,risk,execution,cost,watchdog")
    ap.add_argument("--python", default=None, help="Interpreter (Default: .venv/bin/python, sonst aktuelles Python)")
    ap.add_argument("--synth", action="store_true", help="Offline-Selbsttest gegen den Synth-Broker (data/synth)")
    ap.add_argument("--no-llm", action="store_true", help="LLM-Schritte überspringen (deterministisch)")
    ap.add_argument("--data-dir", default=None, help="Runtime-State isolieren (z.B. data/synth)")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--timeout", type=int, default=900, help="Max. Sekunden pro Schritt")
    args = ap.parse_args(argv)

    env = os.environ.copy()
    if args.synth:
        env["ZHF_SYNTH"] = "1"
        env.setdefault("ZHF_SKIP_LLM", "1")
        args.data_dir = args.data_dir or "data/synth"
    if args.no_llm:
        env["ZHF_SKIP_LLM"] = "1"
    if args.data_dir:
        env["ZHF_DATA_DIR"] = str(Path(args.data_dir) if Path(args.data_dir).is_absolute()
                                  else ROOT / args.data_dir)
    data_dir = Path(env.get("ZHF_DATA_DIR", ROOT / "data"))

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    steps = [s for s in STEPS if not only or s[0] in only]
    py = _python(args.python)
    print(f"=== ZHF Pipeline start – {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"    python={py}\n    data_dir={data_dir}\n    steps={' → '.join(s[0] for s in steps)}"
          + ("    [SYNTH-Modus: keine echten Broker/Orders]" if args.synth else ""))
    overall_t0 = time.time()
    failures: list[str] = []
    for name, mod_args in steps:
        t0 = time.time()
        print(f"\n--- Running {name} ---", flush=True)
        try:
            res = subprocess.run([py, *mod_args], cwd=ROOT, env=env,
                                 timeout=args.timeout)
            rc = res.returncode
        except FileNotFoundError:
            print(f"[FAIL] {name}: Interpreter nicht gefunden: {py} (--pythonpfad angeben)")
            failures.append(name)
            break
        except subprocess.TimeoutExpired:
            print(f"[FAIL] {name}: Timeout nach {args.timeout}s (hängt evtl. an "
                  f"OpenCode-CLI oder Broker-API – OPENCODE_TIMEOUT/HTTP_TIMEOUT_S prüfen)")
            rc = 124
        dt = time.time() - t0
        if rc != 0:
            print(f"[WARN] {name} exited with code {rc} after {dt:.1f}s")
            failures.append(name)
            if args.fail_fast:
                break
        else:
            print(f"[OK]   {name} completed in {dt:.1f}s")

    print("\n=== Heartbeats ===")
    for agent, info in _heartbeat_summary(data_dir).items():
        mark = "✔" if info["status"] == "ok" else ("…" if info["status"] in ("idle", "never_run") else "✖")
        print(f"  {mark} {agent:<14} {info['status']:<9} {info['message']}")
    print(f"\n=== Pipeline done in {time.time() - overall_t0:.1f}s"
          + (f" – Fehler in: {', '.join(failures)}" if failures else " – alles grün") + " ===")
    if failures:
        print("Nächste Schritte: data/logs/<agent>.log und "
              f"{data_dir / 'market_data' / 'status.json'} ansehen.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
