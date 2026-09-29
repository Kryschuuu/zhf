"""OpenCode Zen model discovery and safe per-agent fallback selection.

Model ids in Paperclip/OpenCode use ``provider/model`` form.  The Zen catalog
can change independently of this repository, so callers should not assume that
every preferred id is still available.  We inspect ``opencode models opencode``
when possible and choose the first configured fallback that is actually listed.
If the catalog cannot be queried (offline, missing CLI, or transient error), we
retain the preferred id and let the normal OpenCode-to-local fallback handle
the request.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# Ordered strongest/most appropriate first. The first entry is the normal
# preference; subsequent entries are known Zen alternatives, not model aliases.
MODEL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "ceo": (
        "nemotron-3-ultra-free",
        "nemotron-3-super-free",
        "longcat-2.5-preview-free",
        "big-pickle",
        "space-bunny-free",
        "nemotron-3.5-lightning-free",
    ),
    "research": (
        "big-pickle",
        "mimo-v2-pro-free",
        "minimax-m2.5-free",
        "longcat-2.5-preview-free",
        "space-bunny-free",
        "nemotron-3.5-lightning-free",
    ),
    "risk": (
        "minimax-m2.5-free",
        "big-pickle",
        "nemotron-3-super-free",
        "space-bunny-free",
        "nemotron-3.5-lightning-free",
    ),
    "backtest": (
        "gpt-5-nano",
        "mimo-v2.5-flash-free",
        "big-pickle",
        "nemotron-3.5-lightning-free",
    ),
    "cost": (
        "mimo-v2.5-flash-free",
        "gpt-5-nano",
        "space-bunny-free",
        "big-pickle",
        "nemotron-3.5-lightning-free",
    ),
    "default": ("big-pickle", "mimo-v2-pro-free", "space-bunny-free"),
}

_AGENT_ENV: dict[str, tuple[str, ...]] = {
    "ceo": ("OPENCODE_MODEL_CEO",),
    "research": ("OPENCODE_MODEL_RESEARCH",),
    "risk": ("OPENCODE_MODEL_RISK",),
    "backtest": ("OPENCODE_MODEL_BACKTEST", "OPENCODE_MODEL_REVIEW"),
    "cost": ("OPENCODE_MODEL_COST",),
    "default": ("OPENCODE_MODEL_DEFAULT",),
}

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOCAL_LLM_CANDIDATES = (
    ("lmstudio", "http://127.0.0.1:1234/v1"),
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("llamacpp", "http://127.0.0.1:8080/v1"),
    ("vllm", "http://127.0.0.1:8000/v1"),
)
_CATALOG_CACHE: dict[str, Any] | None = None
_CATALOG_CACHE_AT = 0.0
_CATALOG_CACHE_TTL_S = 300.0


def _read_dotenv_value(key: str) -> str | None:
    """Read one simple KEY=value from the repo .env without importing dotenv."""
    path = ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return re.sub(r"\s+#.*$", "", value).strip()
    return None


def _configured_binary() -> str:
    return (os.environ.get("OPENCODE_BIN") or _read_dotenv_value("OPENCODE_BIN") or "opencode").strip()


def _catalog_text_from_file(path_value: str) -> str | None:
    try:
        return Path(path_value).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def discover_local_fallback(*, timeout: float = 0.8) -> dict[str, Any]:
    """Probe known OpenAI-compatible local servers for loaded fallback models."""
    explicit = os.environ.get("LOCAL_LLM_BASE_URL")
    if explicit is None:
        explicit = _read_dotenv_value("LOCAL_LLM_BASE_URL")
    candidates: list[tuple[str, str]] = []
    if explicit and explicit.strip():
        candidates.append(("explicit", explicit.strip().rstrip("/")))
    candidates.extend(_LOCAL_LLM_CANDIDATES)
    api_key = os.environ.get("LOCAL_LLM_API_KEY") or _read_dotenv_value("LOCAL_LLM_API_KEY") or "local"

    for name, base_url in candidates:
        base_url = base_url.rstrip("/")
        # LOCAL_LLM_BASE_URL is configured as an OpenAI API base URL, normally
        # ending in /v1. Avoid sending an Authorization header to a non-local
        # endpoint unless the operator explicitly configured that endpoint.
        headers = {"Accept": "application/json"}
        if name == "explicit" or base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(f"{base_url}/models", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    continue
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
        items = payload.get("data", []) if isinstance(payload, dict) else []
        model_ids = sorted({
            str(item.get("id")) for item in items
            if isinstance(item, dict) and item.get("id")
        }) if isinstance(items, list) else []
        if model_ids:
            return {"status": "available", "provider": name, "models": model_ids}
    return {"status": "unavailable", "provider": None, "models": []}


def _cache_catalog(result: dict[str, Any]) -> dict[str, Any]:
    global _CATALOG_CACHE, _CATALOG_CACHE_AT
    _CATALOG_CACHE = result
    _CATALOG_CACHE_AT = time.monotonic()
    return dict(result)


def discover_catalog(*, timeout: float = 15.0, force_refresh: bool = False) -> dict[str, Any]:
    """Return a cached catalog result with status, models, and diagnostic text.

    ``ZHF_MODEL_CATALOG_FILE`` allows the setup script to hand a model listing
    obtained inside a Paperclip Docker container to this host-side checker.
    ``ZHF_MODEL_CATALOG_TEXT`` is primarily useful to tests and automation.
    """
    if (
        _CATALOG_CACHE is not None
        and not force_refresh
        and time.monotonic() - _CATALOG_CACHE_AT < _CATALOG_CACHE_TTL_S
    ):
        return dict(_CATALOG_CACHE)

    file_path = os.environ.get("ZHF_MODEL_CATALOG_FILE", "").strip()
    supplied_text = os.environ.get("ZHF_MODEL_CATALOG_TEXT")
    if file_path:
        text = _catalog_text_from_file(file_path)
        if text is None:
            result = {
                "status": "unavailable",
                "models": [],
                "source": "file",
                "message": f"model catalog file could not be read: {file_path}",
            }
        else:
            result = _catalog_result(text, source=f"file:{file_path}")
        return _cache_catalog(result)
    if supplied_text is not None:
        result = _catalog_result(supplied_text, source="environment")
        return _cache_catalog(result)

    binary = _configured_binary()
    executable = shutil.which(binary)
    if not executable and Path(binary).is_file() and os.access(binary, os.X_OK):
        executable = binary
    if not executable:
        result = {
            "status": "unavailable",
            "models": [],
            "source": "cli",
            "message": f"OpenCode CLI '{binary}' was not found in PATH",
        }
        return _cache_catalog(result)

    try:
        proc = subprocess.run(
            [executable, "models", "opencode"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {
            "status": "unavailable",
            "models": [],
            "source": "cli",
            "message": f"OpenCode model catalog could not be queried: {exc}",
        }
        return _cache_catalog(result)

    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        result = {
            "status": "unavailable",
            "models": [],
            "source": "cli",
            "message": f"OpenCode model listing exited with code {proc.returncode}",
        }
    else:
        result = _catalog_result(output, source="opencode models opencode")
    return _cache_catalog(result)


def _catalog_result(text: str, *, source: str) -> dict[str, Any]:
    clean = _ANSI_RE.sub("", text).lower()
    known = sorted({name for names in MODEL_FALLBACKS.values() for name in names})
    available: list[str] = []
    for name in known:
        # Accept both the provider/model form and OpenCode's provider-scoped
        # output where it prints only the model id. Do not substring-match names
        # such as ``gpt-5-nano-plus`` as ``gpt-5-nano``.
        pattern = rf"(?<![a-z0-9._-])(?:opencode/)?{re.escape(name)}(?![a-z0-9._-])"
        if re.search(pattern, clean):
            available.append(f"opencode/{name}")

    status = "available" if available else "empty"
    return {
        "status": status,
        "models": available,
        "source": source,
        "message": "" if available else "catalog responded but no configured Zen model ids were found",
    }


def _normalize_model_id(value: str) -> str:
    value = value.strip()
    if "/" not in value:
        return f"opencode/{value}"
    return value


def _model_override(agent: str) -> str | None:
    for key in _AGENT_ENV.get(agent, _AGENT_ENV["default"]):
        value = os.environ.get(key)
        if value is None:
            value = _read_dotenv_value(key)
        if value and value.strip():
            return _normalize_model_id(value)
    return None


def build_model_report(*, force_refresh: bool = False, include_local: bool = True) -> dict[str, Any]:
    catalog = discover_catalog(force_refresh=force_refresh)
    local_fallback = discover_local_fallback() if include_local else {
        "status": "not-checked", "provider": None, "models": []
    }
    available = set(catalog["models"])
    selected: dict[str, str | None] = {}
    decisions: dict[str, dict[str, Any]] = {}

    for agent, fallback_ids in MODEL_FALLBACKS.items():
        override = _model_override(agent)
        candidates = ([override] if override else []) + [f"opencode/{m}" for m in fallback_ids]
        # Preserve order while removing repeated primary/override ids.
        candidates = list(dict.fromkeys(candidates))
        if catalog["status"] == "unavailable":
            choice = candidates[0]
            reason = "catalog-unverified"
        else:
            choice = next((model for model in candidates if model in available), None)
            reason = "preferred" if choice == f"opencode/{fallback_ids[0]}" and not override else (
                "configured-override" if choice == override and override else
                "fallback" if choice else "no-model")

        selected[agent] = choice
        decisions[agent] = {
            "model": choice,
            "reason": reason,
            "preferred": f"opencode/{fallback_ids[0]}",
            "override": override,
        }

    return {
        "catalog_status": catalog["status"],
        "catalog_source": catalog["source"],
        "catalog_message": catalog["message"],
        "available_models": sorted(available),
        "local_fallback": local_fallback,
        "selected_models": selected,
        "agents": decisions,
    }


def select_agent_model(agent: str) -> str | None:
    """Select the preferred available OpenCode model, or ``None`` if none exist.

    If catalog discovery itself is unavailable, return the configured preferred
    model so that OpenCode can still be attempted and the local LLM fallback can
    take over on failure.
    """
    normalized = agent.strip().lower()
    if normalized not in MODEL_FALLBACKS:
        normalized = "default"
    return build_model_report(include_local=False)["selected_models"].get(normalized)


def reset_model_cache() -> None:
    """Reset the catalog cache. Useful after changing provider config or tests."""
    global _CATALOG_CACHE, _CATALOG_CACHE_AT
    _CATALOG_CACHE = None
    _CATALOG_CACHE_AT = 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check OpenCode Zen models and fallback selection.")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument("--refresh", action="store_true", help="query the OpenCode catalog again")
    parser.add_argument(
        "--skip-local-probe",
        action="store_true",
        help="do not probe local LLM endpoints (use when checking a different runtime host)",
    )
    args = parser.parse_args(argv)
    report = build_model_report(force_refresh=args.refresh, include_local=not args.skip_local_probe)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"OpenCode catalog: {report['catalog_status']} ({report['catalog_source']})")
    if report["catalog_message"]:
        print(f"  {report['catalog_message']}")
    for agent in ("ceo", "research", "risk", "backtest", "cost"):
        decision = report["agents"][agent]
        model = decision["model"] or "none"
        print(f"  {agent:<9} {model} [{decision['reason']}]")
    local = report["local_fallback"]
    if local["status"] == "available":
        print(f"  local     {local['provider']} ({len(local['models'])} model(s) loaded)")
    else:
        print("  local     no compatible local LLM endpoint detected")
    return 0 if report["catalog_status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
