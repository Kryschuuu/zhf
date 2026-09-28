"""Shared state via JSON files – die Agenten kommunizieren über das Dateisystem.

Papierclip selbst bietet eine Ticketing/Timeline – als Low-Level-Transport
zwischen den prozess-basierten Agenten verwenden wir aber einfache atomare
JSON-Dateien. Das ist robust, debug-bar und braucht keinen laufenden Server.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import cfg
from .logger import get_logger

log = get_logger("state")


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open() as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not read %s: %s", path, e)
    return default


@dataclass
class Heartbeat:
    agent: str
    last_run: str
    status: str  # ok | error | idle
    message: str = ""
    duration_s: float = 0.0

    def write(self) -> None:
        p = cfg.DATA_DIR / "heartbeats" / f"{self.agent}.json"
        _atomic_write(p, asdict(self))

    @classmethod
    def read(cls, agent: str) -> "Heartbeat":
        p = cfg.DATA_DIR / "heartbeats" / f"{agent}.json"
        d = _read_json(p, None)
        if d is None:
            return cls(agent=agent, last_run="", status="never_run", message="no heartbeat yet")
        return cls(**d)


# ---------------------------------------------------------------------------
# Shared State Dateien
# ---------------------------------------------------------------------------

class SharedState:
    # Research → Backtest/Risk
    CANDIDATES = cfg.DATA_DIR / "signals" / "candidates.json"
    # Backtest → Risk: mit Metriken angereicherte Signale
    VALIDATED = cfg.DATA_DIR / "signals" / "validated.json"
    # Risk → Execution: freigegebene Orders
    APPROVED_TRADES = cfg.DATA_DIR / "orders" / "approved.json"
    # Execution → alle: Order-Status
    ORDER_STATE = cfg.DATA_DIR / "orders" / "state.json"
    # Cost Optimizer
    FEE_REPORT = cfg.DATA_DIR / "reports" / "fee_report.json"
    # CEO daily report
    DAILY_REPORT = cfg.DATA_DIR / "reports" / "daily_report.md"
    # Portfolio snapshot
    PORTFOLIO = cfg.DATA_DIR / "portfolio.json"
    # System health flag – Execution prüft dies vor jedem Order
    KILLSWITCH = cfg.DATA_DIR / "killswitch.json"

    @classmethod
    def killswitch_active(cls) -> tuple[bool, str]:
        d = _read_json(cls.KILLSWITCH, {"active": False, "reason": ""})
        return bool(d.get("active", False)), d.get("reason", "")

    @classmethod
    def activate_killswitch(cls, reason: str) -> None:
        log.critical("KILLSWITCH ACTIVATED: %s", reason)
        _atomic_write(cls.KILLSWITCH, {
            "active": True,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        })

    @classmethod
    def deactivate_killswitch(cls) -> None:
        _atomic_write(cls.KILLSWITCH, {"active": False, "reason": ""})
        log.info("Killswitch deactivated.")

    @classmethod
    def candidates(cls) -> list[dict]:
        return _read_json(cls.CANDIDATES, [])

    @classmethod
    def set_candidates(cls, cands: list[dict]) -> None:
        _atomic_write(cls.CANDIDATES, {"generated_at": datetime.now(timezone.utc).isoformat(), "candidates": cands})

    @classmethod
    def validated(cls) -> list[dict]:
        return _read_json(cls.VALIDATED, {"validated": []}).get("validated", [])

    @classmethod
    def set_validated(cls, vals: list[dict]) -> None:
        _atomic_write(cls.VALIDATED, {"validated_at": datetime.now(timezone.utc).isoformat(), "validated": vals})

    @classmethod
    def approved(cls) -> list[dict]:
        return _read_json(cls.APPROVED_TRADES, {"approved": []}).get("approved", [])

    @classmethod
    def set_approved(cls, trades: list[dict]) -> None:
        _atomic_write(cls.APPROVED_TRADES, {"approved_at": datetime.now(timezone.utc).isoformat(), "approved": trades})

    @classmethod
    def order_state(cls) -> dict:
        return _read_json(cls.ORDER_STATE, {"orders": [], "fills": []})

    @classmethod
    def update_order_state(cls, orders: list[dict], fills: list[dict]) -> None:
        _atomic_write(cls.ORDER_STATE, {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "orders": orders,
            "fills": fills,
        })

    @classmethod
    def portfolio(cls) -> dict:
        return _read_json(cls.PORTFOLIO, {
            "equity": 0.0, "cash": 0.0, "positions": [], "updated_at": "", "source": "unknown",
        })

    @classmethod
    def set_portfolio(cls, eq: float, cash: float, positions: list[dict], source: str) -> None:
        _atomic_write(cls.PORTFOLIO, {
            "equity": eq,
            "cash": cash,
            "positions": positions,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        })


# Beim Laden sicherstellen dass das Heartbeat-Verzeichnis existiert
(cfg.DATA_DIR / "heartbeats").mkdir(parents=True, exist_ok=True)
