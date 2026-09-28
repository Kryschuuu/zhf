"""Run one full pipeline cycle: research → backtest → risk → execution → cost.

Nützlich für manuelle Tests und Initial-Setup. Im Betrieb wird jeder Schritt
einzeln per Paperclip-Heartbeat getriggert.

Usage:
  .venv/bin/python -m scripts.run_pipeline
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"

STEPS = [
    ("research",    ["-m", "scripts.research.run"]),
    ("backtest",    ["-m", "scripts.backtest.run"]),
    ("risk",        ["-m", "scripts.risk.run"]),
    ("execution",   ["-m", "scripts.execution.run"]),
    ("cost",        ["-m", "scripts.cost.run"]),
    ("watchdog",    ["-m", "scripts.monitoring.watchdog"]),
]


def main() -> int:
    print(f"=== ZHF Pipeline start – {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    overall_t0 = time.time()
    for name, args in STEPS:
        t0 = time.time()
        print(f"\n--- Running {name} ---", flush=True)
        res = subprocess.run([str(VENV_PY), *args], cwd=ROOT)
        dt = time.time() - t0
        if res.returncode != 0:
            print(f"[WARN] {name} exited with code {res.returncode} after {dt:.1f}s")
        else:
            print(f"[OK]   {name} completed in {dt:.1f}s")
    print(f"\n=== Pipeline done in {time.time()-overall_t0:.1f}s ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
