"""Pytest-Fixtures: Repo-Root in sys.path, isolierter Datenordner, keine Netze.

Wichtig: `ZHF_DATA_DIR` wird VOR dem ersten Import von `scripts.common.config`
gesetzt, damit kein Test in den echten Runtime-State (data/) schreibt.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="zhf-tests-"))
os.environ["ZHF_DATA_DIR"] = str(_TMP)
os.environ.setdefault("ZHF_SKIP_LLM", "1")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("ALPACA_DATA_MIN_INTERVAL_S", "0")   # Tests nicht drosseln
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: startet einen echten Prozess")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
