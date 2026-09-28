"""Gemeinsames Schema + Lesen/Schreiben des Fills-Logs (data/logs/fills.log).

Execution schreibt, Cost-Optimizer und CEO lesen. Ein Schema an einer Stelle –
vorher erzeugte eine geänderte Spaltenliste still Fehlberechnungen (der
Cost-Optimizer hat unbekannte Spalten schlicht als 0 interpretiert).
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import cfg
from .logger import get_logger

log = get_logger("fills")

FILL_COLUMNS = ["timestamp", "broker", "symbol", "side", "qty", "avg_price", "order_id",
                "status", "strategy", "notional_usd", "ref_price", "slippage_bps",
                "est_fee_usd", "mode"]

FILLS_PATH = cfg.LOG_DIR / "fills.log"


def _path(path: Optional[Path]) -> Path:
    return Path(path) if path else FILLS_PATH


def append_fill(row: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Eine Fill-Zeile anhängen; legt die Datei inkl. Header an und migriert alte Schemata."""
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    needs_header = True
    if p.exists():
        try:
            first = p.open().readline().strip()
            if first and first != ",".join(FILL_COLUMNS):
                # altes Schema archivieren, damit DictReader nicht misliest
                archived = p.with_name(f"{p.stem}.{int(datetime.now(timezone.utc).timestamp())}.old{p.suffix}")
                os.replace(p, archived)
                log.info("fills.log: Schema geändert – alte Datei nach %s verschoben", archived.name)
                p = _path(path)
            elif first:
                needs_header = False
        except OSError as e:
            log.warning("fills.log konnte nicht geprüft werden: %s", e)
    try:
        with p.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FILL_COLUMNS, extrasaction="ignore")
            if needs_header:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in FILL_COLUMNS})
    except OSError as e:
        log.error("fills.log nicht schreibbar: %s", e)
    return p


def read_fills(since: Optional[datetime] = None, until: Optional[datetime] = None,
               path: Optional[Path] = None) -> list[dict]:
    p = _path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with p.open() as f:
            for row in csv.DictReader(f):
                ts = _ts(row.get("timestamp"))
                if ts is None:
                    continue
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
                row["_ts"] = ts
                out.append(row)
    except OSError as e:
        log.warning("fills.log nicht lesbar: %s", e)
    return out


def _ts(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def last_fill(path: Optional[Path] = None) -> dict:
    rows = read_fills(path=path)
    return rows[-1] if rows else {}


def summarise(rows: Iterable[dict]) -> dict:
    n = 0
    notional = 0.0
    fees = 0.0
    for r in rows:
        n += 1
        try:
            notional += float(r.get("notional_usd") or 0)
            fees += float(r.get("est_fee_usd") or 0)
        except (TypeError, ValueError):
            continue
    return {"trades": n, "notional_usd": round(notional, 4), "fees_usd": round(fees, 4)}
