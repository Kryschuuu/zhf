"""Shared configuration loader for ZHF trading system."""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# __file__ = <root>/scripts/common/config.py → 3 Ebenen hoch zum Repo-Root
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


class Config:
    # Root-Pfad des Repos
    ROOT: Path = ROOT

    # Global
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "paper")
    DRY_RUN: bool = _bool(os.getenv("DRY_RUN"), True)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Berlin")

    # OpenCode CLI (PRIMÄR – Zen Free Tier Modelle)
    OPENCODE_BIN: str = os.getenv("OPENCODE_BIN", "opencode")
    OPENCODE_TIMEOUT: int = _int(os.getenv("OPENCODE_TIMEOUT"), 180)
    OPENCODE_DISABLE_TELEMETRY: bool = _bool(os.getenv("OPENCODE_DISABLE_TELEMETRY"), True)

    # Lokaler LLM-Server (FALLBACK – LM Studio, Ollama, llama.cpp, vLLM …
    # Jeder OpenAI-kompatible Endpunkt auf einem der folgenden Ports wird
    # automatisch erkannt. Explizite URL hat Vorrang.)
    LOCAL_LLM_BASE_URL: str = os.getenv(
        "LOCAL_LLM_BASE_URL", ""
    )  # leer = Auto-Detection (LM Studio 1234, Ollama 11434, llama.cpp 8080)
    LOCAL_LLM_API_KEY: str = os.getenv("LOCAL_LLM_API_KEY", "local")
    LOCAL_LLM_TIMEOUT: int = _int(os.getenv("LOCAL_LLM_TIMEOUT"), 120)

    # Alpaca
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
    ALPACA_PAPER: bool = _bool(os.getenv("ALPACA_PAPER"), True)
    ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    ALPACA_DATA_URL: str = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")

    # BingX
    BINGX_API_KEY: str = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET_KEY: str = os.getenv("BINGX_SECRET_KEY", "")
    BINGX_TESTNET: bool = _bool(os.getenv("BINGX_TESTNET"), True)

    # Bitunix
    BITUNIX_API_KEY: str = os.getenv("BITUNIX_API_KEY", "")
    BITUNIX_SECRET_KEY: str = os.getenv("BITUNIX_SECRET_KEY", "")
    BITUNIX_TESTNET: bool = _bool(os.getenv("BITUNIX_TESTNET"), True)

    # Risk
    MAX_POSITION_SIZE_PCT: float = _float(os.getenv("MAX_POSITION_SIZE_PCT"), 2.0)
    MAX_DAILY_DRAWDOWN_PCT: float = _float(os.getenv("MAX_DAILY_DRAWDOWN_PCT"), 3.0)
    MAX_WEEKLY_DRAWDOWN_PCT: float = _float(os.getenv("MAX_WEEKLY_DRAWDOWN_PCT"), 7.0)
    MAX_OPEN_POSITIONS: int = _int(os.getenv("MAX_OPEN_POSITIONS"), 8)
    MAX_LEVERAGE: int = _int(os.getenv("MAX_LEVERAGE"), 3)
    CORRELATION_LIMIT: float = _float(os.getenv("CORRELATION_LIMIT"), 0.7)
    DEFAULT_STOP_LOSS_PCT: float = _float(os.getenv("DEFAULT_STOP_LOSS_PCT"), 1.5)
    DEFAULT_TAKE_PROFIT_PCT: float = _float(os.getenv("DEFAULT_TAKE_PROFIT_PCT"), 3.0)

    # Paths
    DATA_DIR: Path = ROOT / os.getenv("DATA_DIR", "data")
    REPORTS_DIR: Path = ROOT / os.getenv("REPORTS_DIR", "data/reports")
    LOG_DIR: Path = ROOT / os.getenv("LOG_DIR", "data/logs")
    STRATEGIES_DIR: Path = ROOT / os.getenv("STRATEGIES_DIR", "strategies")

    @classmethod
    def ensure_dirs(cls) -> None:
        for d in (cls.DATA_DIR, cls.REPORTS_DIR, cls.LOG_DIR, cls.STRATEGIES_DIR):
            d.mkdir(parents=True, exist_ok=True)
            (cls.DATA_DIR / "market_data").mkdir(exist_ok=True)
            (cls.DATA_DIR / "backtest_results").mkdir(exist_ok=True)
            (cls.DATA_DIR / "signals").mkdir(exist_ok=True)
            (cls.DATA_DIR / "orders").mkdir(exist_ok=True)


cfg = Config()
cfg.ensure_dirs()
