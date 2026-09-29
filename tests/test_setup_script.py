from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_setup_entrypoints_parse_and_help():
    for script in (ROOT / "setup-script.sh", ROOT / "scripts" / "setup.sh"):
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        ["bash", str(ROOT / "setup-script.sh"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert help_result.returncode == 0
    assert "setup-script.fish" in help_result.stdout
    assert "--provision-paperclip" in help_result.stdout
    assert "echte Broker-Orders" in help_result.stdout


def test_setup_rejects_unknown_mode_without_side_effects():
    result = subprocess.run(
        ["bash", str(ROOT / "setup-script.sh"), "--mode", "invalid"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "Ungültiger Modus" in result.stdout


def test_setup_rejects_unknown_invite_role_before_side_effects():
    result = subprocess.run(
        ["bash", str(ROOT / "setup-script.sh"), "--mode", "none", "--invite-role", "superuser"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "Ungültige Einladungsrolle" in result.stdout
