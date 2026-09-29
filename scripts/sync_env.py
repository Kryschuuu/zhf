#!/usr/bin/env python3
"""Vergleicht .env mit .env.example und ergänzt fehlende Schlüssel (überschreibt nichts).

Hintergrund: neue Konfig-Schlüssel (ALPACA_DATA_FEED, BITUNIX_*, DROP_PARTIAL_BAR, …)
tauchen in bestehende .env-Dateien nie auf. Die Defaults in scripts/common/config.py
decken alles ab, aber ein synchronisiertes .env ist selbsterklärender.

    python -m scripts.sync_env            # ergänzen
    python -m scripts.sync_env --check    # nur prüfen, Exit 1 bei fehlenden Schlüsseln
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"
KEY = re.compile(r"^([A-Z0-9_]{3,})=(.*)$")


def _keys(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = KEY.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="nur prüfen, nichts schreiben")
    args = ap.parse_args(argv)

    if not TEMPLATE.exists():
        print(f"FEHLER: {TEMPLATE} fehlt", file=sys.stderr)
        return 2
    want, have = _keys(TEMPLATE), _keys(ENV)
    # Older template lines used `KEY=   # comment`; python-dotenv treats the
    # comment text as the value when whitespace follows `=`. For known blank
    # URL/path settings this is invalid, so repair only that exact placeholder
    # shape while preserving every real operator-supplied value.
    invalid_blank = [
        key for key, default in want.items()
        if not default.strip() and key in have and have[key].lstrip().startswith("#")
    ]
    if invalid_blank and args.check:
        print(f"UNGÜLTIGE leere .env-Platzhalter: {', '.join(invalid_blank)}")
        return 1
    if invalid_blank:
        lines = ENV.read_text(encoding="utf-8").splitlines()
        repaired = []
        for line in lines:
            match = KEY.match(line.strip())
            if match and match.group(1) in invalid_blank and match.group(2).lstrip().startswith("#"):
                repaired.append(f"{match.group(1)}=")
            else:
                repaired.append(line)
        ENV.write_text("\n".join(repaired) + "\n", encoding="utf-8")
        print(f".env bereinigt: {', '.join(invalid_blank)} (ungültige Inline-Kommentare entfernt)")
        have = _keys(ENV)

    missing = [(k, v) for k, v in want.items() if k not in have]
    unknown = sorted(set(have) - set(want))

    if not missing:
        print(f"OK: .env enthält alle {len(want)} Schlüssel aus .env.example"
              + (f" (eigene Einträge: {', '.join(unknown)})" if unknown else ""))
        return 0
    if args.check:
        print(f"FEHLEND in .env: {', '.join(k for k, _ in missing)}")
        return 1

    with ENV.open("a", encoding="utf-8") as f:
        f.write("\n# ── ergänzt von scripts/sync_env.py "
                f"({Path(__file__).name}) ──\n")
        for k, v in missing:
            f.write(f"{k}={v}\n")
    print(f".env ergänzt: {', '.join(k for k, _ in missing)}  (Werte = Defaults, bitte prüfen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
