from __future__ import annotations

import subprocess

from scripts.common import models


def test_catalog_parser_matches_exact_configured_ids():
    parsed = models._catalog_result(
        "\x1b[32mopencode/big-pickle\x1b[0m\nopencode/gpt-5-nano-plus\n",
        source="test",
    )
    assert parsed["status"] == "available"
    assert parsed["models"] == ["opencode/big-pickle"]


def test_fallback_selection_uses_first_available_per_agent(monkeypatch):
    monkeypatch.setattr(
        models,
        "discover_catalog",
        lambda **_kwargs: {
            "status": "available",
            "models": [
                "opencode/nemotron-3-super-free",
                "opencode/big-pickle",
                "opencode/gpt-5-nano",
            ],
            "source": "test",
            "message": "",
        },
    )
    monkeypatch.setattr(
        models,
        "discover_local_fallback",
        lambda **_kwargs: {"status": "unavailable", "provider": None, "models": []},
    )
    report = models.build_model_report()
    assert report["selected_models"]["ceo"] == "opencode/nemotron-3-super-free"
    assert report["agents"]["ceo"]["reason"] == "fallback"
    assert report["selected_models"]["research"] == "opencode/big-pickle"
    assert report["selected_models"]["risk"] == "opencode/big-pickle"
    assert report["selected_models"]["backtest"] == "opencode/gpt-5-nano"


def test_model_override_is_used_only_when_listed(monkeypatch):
    monkeypatch.setenv("OPENCODE_MODEL_RISK", "opencode/big-pickle")
    monkeypatch.setattr(
        models,
        "discover_catalog",
        lambda **_kwargs: {
            "status": "available",
            "models": ["opencode/big-pickle", "opencode/minimax-m2.5-free"],
            "source": "test",
            "message": "",
        },
    )
    monkeypatch.setattr(models, "discover_local_fallback", lambda **_kwargs: {})
    report = models.build_model_report()
    assert report["selected_models"]["risk"] == "opencode/big-pickle"
    assert report["agents"]["risk"]["reason"] == "configured-override"


def test_catalog_unavailable_keeps_preference_for_runtime_attempt(monkeypatch):
    monkeypatch.setattr(
        models,
        "discover_catalog",
        lambda **_kwargs: {
            "status": "unavailable",
            "models": [],
            "source": "test",
            "message": "offline",
        },
    )
    monkeypatch.setattr(models, "discover_local_fallback", lambda **_kwargs: {})
    assert models.select_agent_model("ceo") == "opencode/nemotron-3-ultra-free"


def test_successful_empty_catalog_has_no_open_code_model(monkeypatch):
    monkeypatch.setattr(
        models,
        "discover_catalog",
        lambda **_kwargs: {
            "status": "empty",
            "models": [],
            "source": "test",
            "message": "no known ids",
        },
    )
    monkeypatch.setattr(models, "discover_local_fallback", lambda **_kwargs: {})
    assert models.select_agent_model("research") is None


def test_model_report_can_skip_host_local_probe_for_container_runtime(monkeypatch):
    monkeypatch.setattr(
        models,
        "discover_catalog",
        lambda **_kwargs: {
            "status": "available",
            "models": ["opencode/big-pickle"],
            "source": "test",
            "message": "",
        },
    )

    def unexpected_probe():
        raise AssertionError("host-local endpoints must not be reported for the container runtime")

    monkeypatch.setattr(models, "discover_local_fallback", unexpected_probe)
    report = models.build_model_report(include_local=False)
    assert report["local_fallback"]["status"] == "not-checked"


def test_local_fallback_discovery_reads_loaded_models(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-test")
    monkeypatch.setattr(models, "_LOCAL_LLM_CANDIDATES", ())

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"qwen2.5-7b"}]}'

    requests = []

    def open_request(request, timeout):
        requests.append((request.full_url, request.headers, timeout))
        return Response()

    monkeypatch.setattr(models.urllib.request, "urlopen", open_request)
    report = models.discover_local_fallback()
    assert report == {"status": "available", "provider": "explicit", "models": ["qwen2.5-7b"]}
    assert requests[0][0] == "http://localhost:1234/v1/models"
    assert requests[0][2] > 0


def test_catalog_query_uses_provider_scoped_cli_and_caches(monkeypatch):
    models.reset_model_cache()
    calls = []

    class Result:
        returncode = 0
        stdout = "opencode/big-pickle\nopencode/mimo-v2-pro-free"
        stderr = ""

    monkeypatch.setattr(models.shutil, "which", lambda _binary: "/usr/bin/opencode")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(subprocess, "run", run)
    try:
        first = models.discover_catalog(force_refresh=True)
        second = models.discover_catalog()
        assert first["status"] == "available"
        assert first["models"] == ["opencode/big-pickle", "opencode/mimo-v2-pro-free"]
        assert second == first
        assert len(calls) == 1
        assert calls[0][0][1:] == ["models", "opencode"]
    finally:
        models.reset_model_cache()
