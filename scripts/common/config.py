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


def _path(v: str | None, default: Path) -> Path:
    """Relativen Pfad immer am Repo-Root verankern (unabhängig von der CWD)."""
    if not v:
        return default
    p = Path(v).expanduser()
    return p if p.is_absolute() else (ROOT / p)


class Config:
    # Root-Pfad des Repos
    ROOT: Path = ROOT

    # Global
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "paper")
    DRY_RUN: bool = _bool(os.getenv("DRY_RUN"), True)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Berlin")

    # Synthetischer Offline-Modus: nutzt den eingebauten Fake-Broker statt
    # Alpaca/BingX/Bitunix. Damit lässt sich die Pipeline ohne API-Keys,
    # ohne Netz und ohne reale Orders testen (data/synth/ ist isoliert).
    SYNTH: bool = _bool(os.getenv("ZHF_SYNTH"), False)

    # OpenCode CLI (PRIMÄR – Zen Free Tier Modelle)
    OPENCODE_BIN: str = os.getenv("OPENCODE_BIN", "opencode")
    OPENCODE_TIMEOUT: int = _int(os.getenv("OPENCODE_TIMEOUT"), 180)
    OPENCODE_DISABLE_TELEMETRY: bool = _bool(os.getenv("OPENCODE_DISABLE_TELEMETRY"), True)
    # Tests/Selbstläufe: LLM-Aufrufe komplett überspringen (deterministische
    # Regel-Pipeline). Ein nicht antwortendes Modell darf nie Trades erfinden.
    SKIP_LLM: bool = _bool(os.getenv("ZHF_SKIP_LLM"), False)
    SYNTH_SEED: int = _int(os.getenv("SYNTH_SEED"), 42)

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
    # Markt-Daten-Feed. "auto" = IEX (Free-Plan). "sip" nur mit bezahltem Abo
    # (sonst: HTTP 403 "subscription does not permit querying recent SIP data").
    ALPACA_DATA_FEED: str = (os.getenv("ALPACA_DATA_FEED", "auto") or "auto").strip().lower()
    # Letzte (noch offene) Bar verwerfen – sonst rechnet der Research mit einem
    # halbfertigen Candle (RSI/MACD/Entry-Preis verfälscht).
    ALPACA_DROP_PARTIAL_BAR: bool = _bool(os.getenv("ALPACA_DROP_PARTIAL_BAR"), True)
    # Mindestabstand zwischen Data-Calls (Free-Tier: 200 req/min pro Konto).
    ALPACA_DATA_MIN_INTERVAL_S: float = _float(os.getenv("ALPACA_DATA_MIN_INTERVAL_S"), 0.35)
    ALPACA_MAX_RETRIES: int = _int(os.getenv("ALPACA_MAX_RETRIES"), 3)
    ALPACA_EXTENDED_HOURS: bool = _bool(os.getenv("ALPACA_EXTENDED_HOURS"), False)
    # OTO-Bracket (SL/TP) direkt am Broker setzen – für Aktien unterstützt.
    ALPACA_BRACKET_ORDERS: bool = _bool(os.getenv("ALPACA_BRACKET_ORDERS"), True)

    # BingX
    BINGX_API_KEY: str = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET_KEY: str = os.getenv("BINGX_SECRET_KEY", "")
    BINGX_TESTNET: bool = _bool(os.getenv("BINGX_TESTNET"), True)
    BINGX_BASE_URL: str = os.getenv("BINGX_BASE_URL", "")  # leer = ccxt-Default

    # Bitunix
    BITUNIX_API_KEY: str = os.getenv("BITUNIX_API_KEY", "")
    BITUNIX_SECRET_KEY: str = os.getenv("BITUNIX_SECRET_KEY", "")
    # Bitunix betreibt KEIN öffentliches Testnet (nur "Demo Trading" im Web-UI,
    # ohne API). Der einzige REST-Endpoint ist https://fapi.bitunix.com.
    # BITUNIX_TESTNET=true schaltet deshalb auf "read-only": Marktdaten ja,
    # Orders werden blockiert, solange nicht explizit freigeschaltet.
    BITUNIX_TESTNET: bool = _bool(os.getenv("BITUNIX_TESTNET"), True)
    BITUNIX_ALLOW_LIVE_ORDERS: bool = _bool(os.getenv("BITUNIX_ALLOW_LIVE_ORDERS"), False)
    BITUNIX_BASE_URL: str = os.getenv("BITUNIX_BASE_URL", "https://fapi.bitunix.com")
    BITUNIX_MARGIN_COIN: str = os.getenv("BITUNIX_MARGIN_COIN", "USDT")

    # Netzwerk-Härtung (alle Broker)
    # Nach N Fehlern in Folge wird ein Broker für COOLDOWN Sekunden nicht mehr
    # abgefragt – verhindert Log-Spam und Timeouts im Sekundentakt.
    BROKER_MAX_CONSECUTIVE_ERRORS: int = _int(os.getenv("BROKER_MAX_CONSECUTIVE_ERRORS"), 3)
    BROKER_COOLDOWN_S: int = _int(os.getenv("BROKER_COOLDOWN_S"), 300)
    HTTP_TIMEOUT_S: float = _float(os.getenv("HTTP_TIMEOUT_S"), 15.0)
    HTTP_MAX_RETRIES: int = _int(os.getenv("HTTP_MAX_RETRIES"), 3)

    # Risk
    MAX_POSITION_SIZE_PCT: float = _float(os.getenv("MAX_POSITION_SIZE_PCT"), 2.0)
    MAX_DAILY_DRAWDOWN_PCT: float = _float(os.getenv("MAX_DAILY_DRAWDOWN_PCT"), 3.0)
    MAX_WEEKLY_DRAWDOWN_PCT: float = _float(os.getenv("MAX_WEEKLY_DRAWDOWN_PCT"), 7.0)
    MAX_OPEN_POSITIONS: int = _int(os.getenv("MAX_OPEN_POSITIONS"), 8)
    MAX_LEVERAGE: int = _int(os.getenv("MAX_LEVERAGE"), 3)
    CORRELATION_LIMIT: float = _float(os.getenv("CORRELATION_LIMIT"), 0.7)
    DEFAULT_STOP_LOSS_PCT: float = _float(os.getenv("DEFAULT_STOP_LOSS_PCT"), 1.5)
    DEFAULT_TAKE_PROFIT_PCT: float = _float(os.getenv("DEFAULT_TAKE_PROFIT_PCT"), 3.0)
    # Harte Obergrenze für die Positionsgrösse als Anteil des Equity (Notional,
    # nicht Risiko!). MAX_POSITION_SIZE_PCT begrenzt den *Verlust* pro Trade.
    MAX_POSITION_NOTIONAL_PCT: float = _float(os.getenv("MAX_POSITION_NOTIONAL_PCT"), 20.0)
    # Mindest-Notional pro Order (USD). Broker-Minima: Alpaca ~$1, BingX/Bitunix ~5 USDT.
    MIN_ORDER_NOTIONAL_USD: float = _float(os.getenv("MIN_ORDER_NOTIONAL_USD"), 5.0)

    # Paths – DATA_DIR isoliert den kompletten Runtime-State (auch Logs,
    # Reports, Orders). ZHF_SYNTH nutzt automatisch data/synth, damit ein
    # Pipeline-Selbsttest den echten State nicht überschreibt.
    DATA_DIR: Path = _path(os.getenv("ZHF_DATA_DIR") or os.getenv("DATA_DIR"),
                           ROOT / ("data/synth" if _bool(os.getenv("ZHF_SYNTH"), False) else "data"))
    REPORTS_DIR: Path = _path(os.getenv("REPORTS_DIR"), DATA_DIR / "reports")
    LOG_DIR: Path = _path(os.getenv("LOG_DIR"), DATA_DIR / "logs")
    STRATEGIES_DIR: Path = _path(os.getenv("STRATEGIES_DIR"), ROOT / "strategies")
    PROMPTS_DIR: Path = _path(os.getenv("PROMPTS_DIR"), ROOT / "prompts")

    @classmethod
    def ensure_dirs(cls) -> None:
        for d in (cls.DATA_DIR, cls.REPORTS_DIR, cls.LOG_DIR,
                  cls.DATA_DIR / "market_data", cls.DATA_DIR / "backtest_results",
                  cls.DATA_DIR / "signals", cls.DATA_DIR / "orders",
                  cls.DATA_DIR / "heartbeats"):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def redacted(cls) -> dict:
        """Konfiguration ohne Secrets – für Watchdog/CEO-Reports."""
        return {
            "ENVIRONMENT": cls.ENVIRONMENT,
            "DRY_RUN": cls.DRY_RUN,
            "SYNTH": cls.SYNTH,
            "LOG_LEVEL": cls.LOG_LEVEL,
            "ALPACA_PAPER": cls.ALPACA_PAPER,
            "ALPACA_DATA_FEED": cls.ALPACA_DATA_FEED,
            "BINGX_TESTNET": cls.BINGX_TESTNET,
            "BITUNIX_TESTNET": cls.BITUNIX_TESTNET,
            "BITUNIX_BASE_URL": cls.BITUNIX_BASE_URL,
            "DATA_DIR": str(cls.DATA_DIR.relative_to(ROOT) if str(cls.DATA_DIR).startswith(str(ROOT)) else cls.DATA_DIR),
        }


cfg = Config()
cfg.ensure_dirs()
