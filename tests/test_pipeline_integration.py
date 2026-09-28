"""Integration: der synthetische Selbsttest muss die ganze Pipeline durchlaufen.

Startet `python -m scripts.monitoring.synth_test` als echten Prozess in einem
temporären Datenverzeichnis. Das ist der Regressions-Schutz für die gemeldeten
Fehler (SYNTH_*-Symbole bei Alpaca, Risk 0/3, Execution ohne Fills).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.integration
def test_synth_self_test_runs_end_to_end(tmp_path):
    data_dir = tmp_path / "synth-state"
    env = {**os.environ, "ZHF_DATA_DIR": str(data_dir), "ZHF_SYNTH": "1",
           "DRY_RUN": "true", "ZHF_SKIP_LLM": "1", "LOG_LEVEL": "WARNING",
           "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "scripts.monitoring.synth_test",
                           "--dir", str(data_dir), "--keep"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "Selbsttest bestanden" in out, out
    # Order-Pfad wurde tatsächlich durchgespielt (Risk → Execution → Fill)
    fills = data_dir / "logs" / "fills.log"
    assert fills.exists(), "fills.log fehlt – Execution hat nichts ausgeführt"
    rows = [l for l in fills.read_text().splitlines()[1:] if l.strip()]
    assert rows, out
    cols = fills.read_text().splitlines()[0].split(",")
    row = dict(zip(cols, rows[-1].split(",")))
    assert float(row["avg_price"]) > 0, "Dry-Run-Fill ohne Preis"
    assert float(row["notional_usd"]) > 0, "Dry-Run-Fill ohne Notional"
    assert row["mode"] == "dry_run"

    report = json.loads((data_dir / "reports" / "fee_report.json").read_text())
    assert report["mode"] == "dry_run"
    assert report["fees"]["trades"] >= 1
    hb = json.loads((data_dir / "heartbeats" / "risk.json").read_text())
    assert hb["status"] == "ok", hb
    assert not (data_dir / "killswitch.json").exists() or \
        json.loads((data_dir / "killswitch.json").read_text())["active"] is False


@pytest.mark.integration
def test_seed_only_does_not_run_agents(tmp_path):
    data_dir = tmp_path / "seeded"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "scripts.monitoring.synth_test",
                           "--dir", str(data_dir), "--seed-only"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cands = json.loads((data_dir / "signals" / "candidates.json").read_text())["candidates"]
    assert cands and all(c["broker"] == "synth" for c in cands), \
        "Synth-Test darf keine Alpaca-Orders auslösen"
    assert not (data_dir / "logs" / "fills.log").exists()


@pytest.mark.integration
def test_pipeline_cli_runs_with_sys_python(tmp_path):
    """`python -m scripts.run_pipeline` ohne .venv darf nicht mit FileNotFoundError sterben."""
    data_dir = tmp_path / "pipe"
    env = {**os.environ, "ZHF_DATA_DIR": str(data_dir), "ZHF_SYNTH": "1", "ZHF_SKIP_LLM": "1",
           "LOG_LEVEL": "WARNING", "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "scripts.run_pipeline",
                           "--only", "risk,execution,cost", "--timeout", "120"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    out = proc.stdout + proc.stderr
    assert "Interpreter nicht gefunden" not in out
    assert "=== Pipeline done" in out, out
    assert "Traceback" not in out, out


@pytest.mark.integration
def test_production_watchdir_is_untouched(tmp_path):
    """Der Selftest schreibt NIE in data/signals – der alte SYNTH_*-Bug."""
    marker = ROOT / "data" / "signals" / "candidates.json"
    before = marker.read_text() if marker.exists() else None
    data_dir = tmp_path / "iso"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ZHF_DATA_DIR": str(data_dir),
           "ZHF_SYNTH": "1", "ZHF_SKIP_LLM": "1", "LOG_LEVEL": "CRITICAL"}
    subprocess.run([sys.executable, "-m", "scripts.monitoring.synth_test",
                    "--dir", str(data_dir), "--keep"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=300)
    after = marker.read_text() if marker.exists() else None
    assert after == before


@pytest.mark.integration
def test_seed_only_keeps_state_full_run_cleans_fresh_dir(tmp_path):
    """""--seed-only' muss den State behalten (man will danach die Pipeline rufen),
    ein voller Lauf räumt nur ein VON IHM angelegtes Verzeichnis weg."""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ZHF_SYNTH": "1",
           "LOG_LEVEL": "CRITICAL"}

    seeded = tmp_path / "seeded"
    subprocess.run([sys.executable, "-m", "scripts.monitoring.synth_test",
                    "--dir", str(seeded), "--seed-only"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=120)
    assert (seeded / "signals" / "validated.json").exists(), \
        "--seed-only muss den State für Folgeläufe stehen lassen"

    fresh = tmp_path / "fresh"
    r = subprocess.run([sys.executable, "-m", "scripts.monitoring.synth_test",
                        "--dir", str(fresh)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not fresh.exists(), "neu erzeugter Test-State hätte aufgeräumt werden müssen"

    existing = tmp_path / "existing"
    existing.mkdir(parents=True, exist_ok=True)
    (existing / "keepme.txt").write_text("wichtig")
    subprocess.run([sys.executable, "-m", "scripts.monitoring.synth_test",
                    "--dir", str(existing)], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=300)
    assert (existing / "keepme.txt").exists(), "vorhandener State darf nicht gelöscht werden"
