#!/usr/bin/env python3
"""Erzeugt Konfigurationsdateien mit dem TATSÄCHLICHEN Repo-Pfad.

`config/paperclip_agents.json` ist die portable ZHF-Blaupause mit relativen
Pfaden. Für manuelle Paperclip-Imports oder Offline-Review erzeugt dieses Skript
eine maschinenspezifische Kopie mit absoluten Pfaden (Checkout-Root und venv).
Der Provisioner `scripts.paperclip_provision` braucht diese Kopie nicht.

    python -m scripts.materialize_config              # → config/paperclip_agents.local.json
    python -m scripts.materialize_config --inplace    # Vorlage selbst aktualisieren
    python -m scripts.materialize_config --check      # nur prüfen (Exit 1 wenn veraltet)
    python -m scripts.materialize_config --docs       # README/docs-Beispiele mit anpassen

Der bisherige Root wird aus den `cwd`-Feldern der Datei gelesen (Mehrheitswert),
ersetzt wird nur ein echter Pfad-Präfix – kein blindes Ersetzen von Substrings.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "config" / "paperclip_agents.json"
DST = ROOT / "config" / "paperclip_agents.local.json"
PATH_KEYS = {"cwd", "system_prompt_file", "command", "path", "log_file", "workdir"}


def previous_root(doc: dict) -> str | None:
    """Der Root, mit dem die Vorlage erzeugt wurde = häufigster cwd-Wert."""
    cwds = [str((a.get("adapter_config") or {}).get("cwd")) for a in doc.get("agents", [])]
    cwds = [c for c in cwds if c and c != "None" and c.startswith("/")]
    if not cwds:
        return None
    return max(set(cwds), key=cwds.count)


def _rewrite(value, old: str, new: str):
    if isinstance(value, dict):
        return {k: (_rewrite(v, old, new) if k in PATH_KEYS or isinstance(v, (dict, list)) else v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, old, new) for v in value]
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old + "/"):
            return new + value[len(old):]
        # "cd /alt/zhf && ..." in Command-Zeilen
        return re.sub(rf"(?<![\w-]){re.escape(old)}(?=/|$|[\s\"'])", new, value)
    return value


def _materialize_paths(doc: dict) -> dict:
    """Resolve the portable blueprint's repo-relative paths for manual import."""
    interpreter = ROOT / ".venv" / "bin" / "python"
    for agent in doc.get("agents", []):
        adapter = agent.get("adapter_config") or {}
        if "cwd" in adapter:
            adapter["cwd"] = str(ROOT)
        prompt = adapter.get("system_prompt_file")
        if prompt:
            prompt_path = Path(str(prompt)).expanduser()
            adapter["system_prompt_file"] = str(
                prompt_path if prompt_path.is_absolute() else (ROOT / prompt_path).resolve()
            )
        command = adapter.get("command")
        if command:
            parts = shlex.split(str(command))
            if parts and (Path(parts[0]).name in ("python", "python3") or parts[0].endswith(".venv/bin/python")):
                adapter["command"] = shlex.join([str(interpreter), *parts[1:]])
        agent["adapter_config"] = adapter
    return doc


def build() -> tuple[dict, str, list[str]]:
    doc = json.loads(SRC.read_text(encoding="utf-8"))
    old = previous_root(doc) or str(ROOT)
    notes: list[str] = []
    if old != str(ROOT):
        doc = _rewrite(doc, old, str(ROOT))
        notes.append(f"Pfadpräfix {old} → {ROOT}")
    else:
        notes.append(f"Pfade stimmen bereits ({ROOT})")

    doc = _materialize_paths(doc)
    notes.append("Repo-relative Prompt-/Interpreter-Pfade für manuellen Import aufgelöst")
    venv = ROOT / ".venv" / "bin" / "python"
    if not venv.exists():
        notes.append(f"WARNUNG: {venv} fehlt – zuerst 'bash scripts/setup.sh'")
    for a in doc.get("agents", []):
        sp = (a.get("adapter_config") or {}).get("system_prompt_file")
        if sp and not Path(sp).exists():
            notes.append(f"WARNUNG: Prompt fehlt: {sp}")
        cmd = (a.get("adapter_config") or {}).get("command")
        if cmd:
            exe = str(cmd).split()[0]
            if exe.startswith("/") and not Path(exe).exists():
                notes.append(f"WARNUNG: Interpreter fehlt: {exe}")
    return doc, old, notes


def _fix_docs(old: str, *, write: bool) -> None:
    pattern = re.compile(rf"(?<![\w-]){re.escape(old)}(?=/|$|[\s`)])")
    for f in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        new = pattern.sub(str(ROOT), text)
        if new == text:
            continue
        if write:
            f.write_text(new, encoding="utf-8")
            print(f"  aktualisiert: {f.relative_to(ROOT)}")
        else:
            print(f"  {f.relative_to(ROOT)}: {len(pattern.findall(text))} veraltete Pfade "
                  f"(--inplace zum Schreiben)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--inplace", action="store_true",
                    help="config/paperclip_agents.json direkt überschreiben")
    ap.add_argument("--check", action="store_true", help="nur prüfen, nichts schreiben")
    ap.add_argument("--docs", action="store_true",
                    help="Beispielpfade in README.md und docs/*.md mit anpassen")
    args = ap.parse_args(argv)

    if not SRC.exists():
        print(f"FEHLER: {SRC} fehlt", file=sys.stderr)
        return 2
    doc, old, notes = build()
    payload = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    target = SRC if args.inplace else DST

    for n in notes:
        print(f"  · {n}")

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        ok = current == payload
        print(("OK: " if ok else "VERALTET: ")
              + f"{target.relative_to(ROOT)} {'entspricht' if ok else 'zeigt auf einen anderen Root'} "
                f"diesem Checkout ({ROOT})")
        if args.docs and not ok:
            _fix_docs(old, write=False)
        return 0 if ok else 1

    target.write_text(payload, encoding="utf-8")
    print(f"geschrieben: {target.relative_to(ROOT)}")
    if args.docs:
        _fix_docs(old, write=args.inplace)
    if not args.inplace:
        print("Die Datei ist eine Pfad-materialisierte Referenz für manuelle Einrichtung: "
              f"{DST.relative_to(ROOT)}. Für automatisierte Provisionierung "
              "`python -m scripts.paperclip_provision` verwenden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
