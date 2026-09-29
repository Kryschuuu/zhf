"""LLM-Client – spricht primär über ein verfügbares OpenCode-Zen-Modell,
mit lokalem LLM-Server (LM Studio / Ollama / llama.cpp / vLLM) als Fallback.

Warum CLI statt direkter HTTP-Aufruf gegen OpenCode?
  - OpenCode ist als `opencode_local`-Adapter in Paperclip integriert.
  - Die Free-Modelle (Big Pickle, Nemotron, MiMo, MiniMax, LongCat, Space Bunny,
    GPT-5-Nano) werden über OpenCode Zen bereitgestellt – die CLI handhabt
    Authentication, Routing und Retry.
  - Bei fehlendem Internet / TLS-Fehlern fällt der Client transparent auf einen
    lokalen OpenAI-kompatiblen Server zurück (LM Studio auf Port 1234, Ollama
    auf Port 11434 oder llama.cpp auf 8080 – automatisch erkannt).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

from .config import cfg
from .logger import get_logger

log = get_logger("llm")


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    via: str  # "opencode" | "local"


# Ports auf denen ein lokaler OpenAI-kompatibler Server lauschen könnte.
# Reihenfolge entspricht Priorität (erster erreichbarer wird verwendet).
LOCAL_LLM_CANDIDATES = [
    ("lmstudio", "http://127.0.0.1:1234/v1"),
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("llamacpp", "http://127.0.0.1:8080/v1"),
    ("vllm", "http://127.0.0.1:8000/v1"),
]

# Cache: einmal erkannter lokaler Endpunkt wird wiederverwendet
_local_endpoint_cache: Optional[tuple[str, str]] = None  # (name, url)


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _extract_text_from_opencode(raw: str) -> str:
    """OpenCode `run` gibt Streaming-Output mit ANSI-Formatierung aus.
    Wir extrahieren die Antwort nach dem letzten Build-Prompt."""
    text = _strip_ansi(raw)
    lines = text.split("\n")
    out_lines: list[str] = []
    in_answer = False
    for line in lines:
        if line.startswith("> build") or line.startswith("▣"):
            in_answer = True
            continue
        if line.startswith("> ") and not in_answer:
            continue
        if line.lower().startswith("error:"):
            raise LLMError(line.strip())
        if in_answer:
            out_lines.append(line)
    result = "\n".join(out_lines).strip()
    if not result:
        result = text.strip()
    # Entferne "Thinking:"-Blöcke und <think>…</think>
    result = re.sub(r"(?:Thinking:|<think>).*?(?:\n\n|</think>|\Z)", "", result, flags=re.S).strip()
    return result


def _discover_local_endpoint() -> Optional[tuple[str, str]]:
    """Prüft LOCAL_LLM_BASE_URL (wenn gesetzt) oder die Standard-Ports,
    ob ein lauffähiger LLM-Server antwortet."""
    global _local_endpoint_cache
    if _local_endpoint_cache is not None:
        return _local_endpoint_cache

    candidates: list[tuple[str, str]] = []
    if cfg.LOCAL_LLM_BASE_URL:
        candidates.append(("explicit", cfg.LOCAL_LLM_BASE_URL.rstrip("/")))
    candidates.extend(LOCAL_LLM_CANDIDATES)

    for name, url in candidates:
        try:
            r = requests.get(f"{url}/models", timeout=1.5,
                             headers={"Authorization": f"Bearer {cfg.LOCAL_LLM_API_KEY}"})
            if r.status_code == 200:
                data = r.json()
                models = data.get("data", [])
                if models:
                    _local_endpoint_cache = (name, url)
                    log.info("Discovered local LLM server: %s at %s (%d models)",
                             name, url, len(models))
                    return _local_endpoint_cache
        except Exception:
            continue
    return None


def call_llm_opencode(prompt: str, system_prompt: Optional[str] = None,
                      agent: str = "default", max_tokens: int = 4096,
                      temperature: float = 0.3) -> LLMResponse:
    """Rufe OpenCode CLI auf und gib Antwort zurück."""
    # Import lazily so `python -m scripts.common.models` doesn't preload the
    # target module through scripts.common.__init__ and trigger a runpy warning.
    from .models import select_agent_model

    model = select_agent_model(agent)
    if model is None:
        raise LLMError(
            f"no configured OpenCode Zen model is available for agent '{agent}'; "
            "check `opencode models opencode` or configure a local LLM fallback"
        )
    cwd = str(cfg.ROOT)
    full_prompt = ""
    if system_prompt:
        full_prompt += f"<system>\n{system_prompt}\n</system>\n\n"
    full_prompt += "<user>\n" + prompt + "\n</user>"

    bin_path = shutil.which(cfg.OPENCODE_BIN)
    if not bin_path:
        raise LLMError(f"opencode binary '{cfg.OPENCODE_BIN}' not found in PATH")

    cmd = [bin_path, "run", "--model", model, "--dir", cwd, "--auto", full_prompt]

    t0 = time.time()
    env = os.environ.copy()
    env["OPENCODE_DISABLE_TELEMETRY"] = "1"
    env.setdefault("NODE_NO_WARNINGS", "1")
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=cfg.OPENCODE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise LLMError(f"opencode call timed out after {cfg.OPENCODE_TIMEOUT}s")

    raw = proc.stdout + "\n" + proc.stderr
    combined = (proc.stderr or "").lower() + (proc.stdout or "").lower()
    if "certificate" in combined or "tls" in combined or "ssl" in combined:
        raise LLMError(f"opencode TLS/network error: {(proc.stderr or '')[:200]}")
    if proc.returncode != 0 and not proc.stdout.strip():
        raise LLMError(f"opencode exited with code {proc.returncode}: {(proc.stderr or '')[:300]}")

    text = _extract_text_from_opencode(raw)
    if not text:
        raise LLMError("opencode returned empty response")
    return LLMResponse(
        text=text, model=model,
        prompt_tokens=0, completion_tokens=0,
        latency_s=time.time() - t0, via="opencode",
    )


def call_llm_local(prompt: str, system_prompt: Optional[str] = None,
                   model: Optional[str] = None, temperature: float = 0.3,
                   max_tokens: int = 2048, max_retries: int = 2) -> LLMResponse:
    """Offline-Fallback via lokalen OpenAI-kompatiblen Server (LM Studio, Ollama, …)."""
    endpoint = _discover_local_endpoint()
    if endpoint is None:
        raise LLMError("No local LLM endpoint reachable (checked LM Studio, Ollama, llama.cpp, vLLM)")
    name, url = endpoint
    chat_url = f"{url}/chat/completions"
    use_model = model  # None = nutze das aktuell geladene Modell
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if use_model:
        payload["model"] = use_model
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg.LOCAL_LLM_API_KEY}"}

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        try:
            r = requests.post(chat_url, headers=headers, json=payload,
                              timeout=cfg.LOCAL_LLM_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if not data.get("choices"):
                raise LLMError(f"local LLM returned no choices: {str(data)[:200]}")
            choice = data["choices"][0]
            msg = choice.get("message", {})
            usage = data.get("usage", {})
            return LLMResponse(
                text=msg.get("content", "").strip(),
                model=data.get("model", use_model or name),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                latency_s=time.time() - t0,
                via=f"local:{name}",
            )
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise LLMError(f"Local LLM ({name}) failed after {max_retries} attempts: {last_err}")


def call_llm(prompt: str, system_prompt: Optional[str] = None,
             agent: str = "default", model: Optional[str] = None,
             temperature: float = 0.3, max_tokens: int = 4096,
             max_retries: int = 2) -> LLMResponse:
    """Primär: OpenCode Zen Free. Fallback: lokaler Server (LM Studio/Ollama)."""
    if getattr(cfg, "SKIP_LLM", False):
        raise LLMError("LLM disabled (ZHF_SKIP_LLM=1) – deterministische Regel-Pipeline")
    try:
        return call_llm_opencode(prompt, system_prompt=system_prompt, agent=agent,
                                 max_tokens=max_tokens, temperature=temperature)
    except LLMError as e:
        log.warning("OpenCode call failed (%s) → falling back to local LLM.", e)
        return call_llm_local(prompt, system_prompt=system_prompt, model=model,
                              temperature=temperature, max_tokens=max_tokens,
                              max_retries=max_retries)


def call_llm_json(prompt: str, system_prompt: Optional[str] = None,
                  agent: str = "default", model: Optional[str] = None,
                  temperature: float = 0.1) -> dict[str, Any]:
    """LLM-Aufruf, der ein JSON-Objekt zurückgibt (best-effort mit Retry)."""
    sys_prompt = (system_prompt or "") + (
        "\n\nANTWORTE AUSSCHLIESSLICH MIT GÜLTIGEM JSON. Keine Einleitungen, "
        "keine Erklärungen, keine Code-Blöcke, keine Denkblöcke. Nur das reine "
        "JSON-Objekt, nichts davor oder danach."
    )
    last_text = ""
    for attempt in range(3):
        resp = call_llm(prompt, system_prompt=sys_prompt, agent=agent, model=model,
                        temperature=temperature, max_tokens=4096)
        text = resp.text.strip()
        last_text = text
        # Markdown-Codeblöcke entfernen
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?\s*```$", "", text)
        # Direkter Parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Regex: erstes { ... }-Objekt extrahieren (gierig, passend geschachtelt)
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        log.warning("LLM returned non-JSON (attempt %d), retrying.", attempt + 1)
        prompt = (
            "Deine vorherige Antwort war kein gültiges JSON. "
            "Antworte ERNEUT und NUR mit einem gültigen JSON-Objekt.\n\n"
            "Ursprüngliche Aufgabe:\n" + prompt + "\n\n"
            "Letzte ungültige Antwort (erste 500 Zeichen):\n" + text[:500]
        )
    raise LLMError(
        f"Could not coerce LLM output to JSON after 3 attempts. "
        f"Last response (first 500 chars): {last_text[:500]}"
    )


def reset_local_cache() -> None:
    """Setzt den Cache des lokalen Endpunkts zurück (z.B. nach Neustart eines Servers)."""
    global _local_endpoint_cache
    _local_endpoint_cache = None
