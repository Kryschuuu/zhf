#!/usr/bin/env python3
"""Idempotently provision the ZHF company in a Paperclip instance.

The Paperclip CLI is used as the supported control-plane client.  Existing
companies, agents, skills, and issues are reused by stable names/slugs; the
script never deletes or overwrites operator-created records.  New agents are
created with heartbeats disabled, and bootstrap issues are unassigned/backlog
items so the provisioning step cannot wake an agent or place an order.

Examples:
    python -m scripts.paperclip_provision --action provision
    python -m scripts.paperclip_provision --action provision --docker-container zhf-paperclip
    python -m scripts.paperclip_provision --action invite --role operator
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from scripts.common.models import select_agent_model
from scripts.common.version import __version__

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "config" / "paperclip_agents.json"
GENERATED_SKILLS = ROOT / "data" / "setup-skills"
DEFAULT_API_BASE = "http://127.0.0.1:3100"
DEFAULT_DOCKER_CONTAINER = "zhf-paperclip"


class ProvisionError(RuntimeError):
    """A Paperclip request or local provisioning step failed."""


def _redact(text: str) -> str:
    """Avoid echoing bearer tokens if a CLI version includes them in an error."""
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", text)
    text = re.sub(r"(?i)(PAPERCLIP_API_KEY\s*[=:]\s*)\S+", r"\1[redacted]", text)
    return text


def _json_from_output(text: str) -> Any:
    """Decode JSON even when a CLI version prints a notice before --json output."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(stripped[index:])
            return value
        except json.JSONDecodeError:
            continue
    return None


def _find_list(payload: Any, preferred_keys: Sequence[str] = ()) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("data", "result", "items", "records"):
        value = payload.get(key)
        if isinstance(value, (list, dict)):
            found = _find_list(value, preferred_keys)
            if found or isinstance(value, list):
                return found
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_list(value, preferred_keys)
            if found:
                return found
    return []


def _find_object(payload: Any, preferred_keys: Sequence[str] = ()) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("id"):
        return payload
    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, dict) and value.get("id"):
            return value
    for key in ("data", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            found = _find_object(value, preferred_keys)
            if found:
                return found
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_object(value, preferred_keys)
            if found:
                return found
    return None


def _key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _skill_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProvisionError(f"cannot read skill file {path}: {exc}") from exc
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", content, flags=re.S)
    if not match:
        raise ProvisionError(f"skill {path.relative_to(ROOT)} has no YAML front matter")
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if field:
            metadata[field.group(1)] = field.group(2).strip().strip("\"'")
    body = match.group(2).lstrip()
    if not metadata.get("name") or not metadata.get("description"):
        raise ProvisionError(f"skill {path.relative_to(ROOT)} requires name and description front matter")
    return metadata, body


def _load_blueprint() -> dict[str, Any]:
    try:
        blueprint = json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError(f"cannot read Paperclip blueprint {BLUEPRINT}: {exc}") from exc
    if not isinstance(blueprint.get("agents"), list) or not blueprint["agents"]:
        raise ProvisionError("Paperclip blueprint must define at least one agent")
    if not isinstance(blueprint.get("tasks"), list):
        raise ProvisionError("Paperclip blueprint must define a tasks list")
    return blueprint


@dataclass
class PaperclipCLI:
    api_base: str = DEFAULT_API_BASE
    docker_container: str | None = None
    binary: str | None = None
    timeout: int = 90

    def __post_init__(self) -> None:
        self.api_base = self.api_base.rstrip("/")
        if self.docker_container:
            self.prefix = [
                "docker", "exec", "--user", "node", self.docker_container,
                "node", "/app/cli/dist/index.js",
            ]
            return
        requested = self.binary or os.environ.get("PAPERCLIP_BIN", "")
        if requested:
            found = shutil.which(requested)
            if not found and Path(requested).is_file() and os.access(requested, os.X_OK):
                found = requested
            if not found:
                raise ProvisionError(f"Paperclip CLI binary not found: {requested}")
            self.prefix = [found]
        elif shutil.which("paperclipai"):
            self.prefix = [shutil.which("paperclipai") or "paperclipai"]
        elif shutil.which("npx"):
            self.prefix = [
                shutil.which("npx") or "npx", "--yes",
                "--registry", "https://registry.npmjs.org", "paperclipai",
            ]
        else:
            raise ProvisionError(
                "Paperclip CLI is unavailable. Install Node.js 24.11+ and use npx, "
                "or select Docker mode with a running ZHF Paperclip container."
            )

    def run(self, args: Sequence[str], *, allow_failure: bool = False) -> Any:
        command = [*self.prefix, *args, "--api-base", self.api_base, "--json"]
        try:
            proc = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProvisionError(f"Paperclip command timed out after {self.timeout}s: {shlex.join(command[:3])}") from exc
        except OSError as exc:
            raise ProvisionError(f"could not execute Paperclip CLI: {exc}") from exc
        if proc.returncode != 0 and not allow_failure:
            details = _redact((proc.stderr or proc.stdout or "").strip())
            if len(details) > 1200:
                details = details[:1200] + "…"
            raise ProvisionError(
                f"Paperclip command failed (exit {proc.returncode}): {shlex.join(command[:4])}"
                + (f"\n{details}" if details else "")
            )
        if proc.returncode != 0:
            return None
        decoded = _json_from_output(proc.stdout)
        if decoded is not None:
            return decoded
        # A non-empty human response is still useful for commands whose JSON
        # payload is not supported in older CLI builds, but must not be treated
        # as a resource record by callers.
        return proc.stdout.strip() or None

    def health(self) -> bool:
        return self.run(["health"], allow_failure=True) is not None


def _resolve_company(cli: PaperclipCLI, blueprint: dict[str, Any], *, create: bool) -> dict[str, Any]:
    name = str(blueprint.get("company") or "zhf-trading")
    existing = _find_list(cli.run(["company", "list"]), ("companies",))
    company = next((item for item in existing if _key(item.get("name")) == _key(name)), None)
    if company:
        return company
    if not create:
        raise ProvisionError(f"Paperclip company '{name}' does not exist yet; run --action provision first")

    payload = {
        "name": name,
        "description": blueprint.get("description", "ZHF multi-agent trading system"),
        "budgetMonthlyCents": 0,
    }
    created = cli.run(["company", "create", "--payload-json", json.dumps(payload, ensure_ascii=False)])
    company = _find_object(created, ("company",))
    if not company or not company.get("id"):
        # Some CLI versions return only a success notice. Re-read the company
        # collection before deciding that the create operation did not work.
        refreshed = _find_list(cli.run(["company", "list"]), ("companies",))
        company = next((item for item in refreshed if _key(item.get("name")) == _key(name)), None)
    if not company or not company.get("id"):
        raise ProvisionError(f"Paperclip company '{name}' was not present after create; refusing to continue")

    # The setup operator is the board and is provisioning the initial roster.
    # The new-agent approval policy is relaxed only around roster creation in
    # provision(), with a finally-protected restore. Never relax an existing
    # company's policy or leave a newly-created company unlocked on an error.
    company["_zhf_created_here"] = True
    return company


def _agent_payload(
    agent: dict[str, Any],
    *,
    ceo_id: str | None,
    runtime_root: str,
    python_path: str,
) -> dict[str, Any]:
    key = str(agent.get("key") or "default").lower()
    adapter_type = str(agent.get("adapter_type") or "process")
    source_config = agent.get("adapter_config") or {}
    budget = int(agent.get("budget_monthly_cents") or 0)
    reports_to = agent.get("reports_to")
    if reports_to in (None, "", "board"):
        parent_id = None
    elif _key(reports_to) == "elara voss":
        parent_id = ceo_id
    else:
        parent_id = None

    payload: dict[str, Any] = {
        "name": str(agent["name"]),
        "role": str(agent.get("key") or agent.get("role") or "general"),
        "title": str(agent.get("title") or agent["name"]),
        "reportsTo": parent_id,
        "capabilities": str(agent.get("capabilities") or agent.get("title") or "ZHF team member"),
        "adapterType": adapter_type,
        "runtimeConfig": {"heartbeat": {"enabled": False}},
        "budgetMonthlyCents": budget,
        "metadata": {"managedBy": "zhf-setup", "zhfVersion": __version__, "agentKey": key},
    }

    if adapter_type == "opencode_local":
        selected = select_agent_model(key)
        if selected is None:
            raise ProvisionError(
                f"no available OpenCode model for agent '{agent.get('name', key)}'; verify `opencode models opencode` "
                "or configure an available OPENCODE_MODEL_* override before provisioning"
            )
        prompt_file = source_config.get("system_prompt_file")
        if prompt_file:
            prompt_path = Path(str(prompt_file)).expanduser()
            candidate = prompt_path if prompt_path.is_absolute() else ROOT / prompt_path
            try:
                relative = candidate.resolve().relative_to(ROOT)
            except ValueError as exc:
                raise ProvisionError(f"prompt path must be inside the ZHF repository: {prompt_file}") from exc
            actual_prompt = (Path(runtime_root) / relative).as_posix()
            if not candidate.is_file():
                raise ProvisionError(f"agent prompt does not exist: {candidate}")
        else:
            actual_prompt = None
        adapter_config: dict[str, Any] = {
            "cwd": runtime_root,
            "model": selected,
            "dangerouslySkipPermissions": True,
            "timeoutSec": 900,
            "env": {"OPENCODE_DISABLE_TELEMETRY": "1"},
        }
        if actual_prompt:
            adapter_config["instructionsFilePath"] = actual_prompt
        payload["adapterConfig"] = adapter_config
        payload["metadata"]["modelSelection"] = selected
    elif adapter_type == "process":
        raw_command = str(source_config.get("command") or "")
        tokens = shlex.split(raw_command)
        if not tokens:
            raise ProvisionError(f"process agent {agent['name']} has no command")
        if "-m" in tokens:
            module_index = tokens.index("-m")
            args = tokens[module_index:]
        else:
            args = tokens[1:]
        env = dict(source_config.get("env") or {})
        model_agent = key if key in ("backtest", "risk", "cost") else None
        if model_agent:
            selected = select_agent_model(model_agent)
            if selected:
                env[f"OPENCODE_MODEL_{model_agent.upper()}"] = selected
                if model_agent == "backtest":
                    env["OPENCODE_MODEL_REVIEW"] = selected
        env.update({
            "PYTHONPATH": runtime_root,
            "ZHF_DATA_DIR": f"{runtime_root}/data",
        })
        adapter_config = {
            "command": python_path,
            "args": args,
            "cwd": runtime_root,
            "env": env,
            "timeoutSec": 900,
            "graceSec": 15,
        }
        payload["adapterConfig"] = adapter_config
    else:
        # Future adapter types should fail closed: silently creating an agent
        # with an unsupported configuration would leave a broken org chart.
        raise ProvisionError(f"unsupported adapter type in ZHF blueprint: {adapter_type}")

    return payload


def _skill_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for path in sorted((ROOT / "agents").glob("**/skills/*.md")):
        metadata, body = _skill_frontmatter(path)
        if not metadata.get("agent"):
            raise ProvisionError(f"skill {path.relative_to(ROOT)} has no agent front matter")
        slug = metadata["name"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ProvisionError(
                f"skill {path.relative_to(ROOT)} has an unsafe/invalid slug {slug!r}; use lowercase letters, digits, and hyphens"
            )
        sources.append({"path": path, "metadata": metadata, "body": body})
    return sources


def _ensure_skills(
    cli: PaperclipCLI,
    company_id: str,
    runtime_root: str,
    agents: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    sources = _skill_sources()
    existing = _find_list(cli.run(["skills", "list", "--company-id", company_id]), ("skills",))
    by_slug = {}
    for item in existing:
        slug = item.get("slug") or item.get("key") or item.get("canonicalKey") or item.get("name")
        if slug:
            by_slug[_key(slug).split("/")[-1]] = item

    created_or_found = 0
    for source in sources:
        metadata = source["metadata"]
        slug = metadata["name"]
        record = by_slug.get(_key(slug))
        if not record:
            GENERATED_SKILLS.mkdir(parents=True, exist_ok=True)
            body_path = GENERATED_SKILLS / f"{slug}.md"
            body_path.write_text(source["body"] + ("\n" if not source["body"].endswith("\n") else ""), encoding="utf-8")
            try:
                rel = body_path.resolve().relative_to(ROOT)
            except ValueError:
                if Path(runtime_root).resolve() != ROOT:
                    raise ProvisionError("generated Paperclip skill body is outside the mounted ZHF checkout")
                cli_body_path = str(body_path.resolve())
            else:
                cli_body_path = (Path(runtime_root) / rel).as_posix()
            command = [
                "skills", "create",
                "--name", slug.replace("-", " ").title(),
                "--slug", slug,
                "--description", metadata["description"],
                "--body-file", cli_body_path,
                "--company-id", company_id,
            ]
            cli.run(command)
            refreshed = _find_list(cli.run(["skills", "list", "--company-id", company_id]), ("skills",))
            by_slug = {}
            for item in refreshed:
                ref = item.get("slug") or item.get("key") or item.get("canonicalKey") or item.get("name")
                if ref:
                    by_slug[_key(ref).split("/")[-1]] = item
            record = by_slug.get(_key(slug))
            if not record:
                raise ProvisionError(f"Paperclip skill '{slug}' was not present after create")
        created_or_found += 1

    attached = 0
    for source in sources:
        metadata = source["metadata"]
        agent_key = metadata["agent"].strip().lower()
        agent_record = agents.get(agent_key)
        if not agent_record:
            raise ProvisionError(f"skill {metadata['name']} refers to missing agent key '{agent_key}'")
        slug = metadata["name"]
        cli.run([
            "skills", "agent", "sync", str(agent_record["id"]),
            "--skill", slug,
            "--mode", "add",
            "--company-id", company_id,
        ])
        attached += 1
    return created_or_found, attached


def _ensure_tasks(cli: PaperclipCLI, company_id: str, blueprint: dict[str, Any]) -> int:
    issues = _find_list(cli.run(["issue", "list", "--company-id", company_id]), ("issues", "tasks"))
    by_title = {_key(issue.get("title")): issue for issue in issues if issue.get("title")}
    created = 0
    for item in blueprint.get("tasks", []):
        title = str(item.get("title") or "").strip()
        if not title:
            raise ProvisionError("Paperclip task blueprint contains an empty title")
        if _key(title) in by_title:
            continue
        payload = {
            "title": title,
            "description": str(item.get("description") or ""),
            "status": "backlog",
            "priority": item.get("priority", "low"),
        }
        cli.run([
            "issue", "create",
            "--company-id", company_id,
            "--title", payload["title"],
            "--description", payload["description"],
            "--status", payload["status"],
            "--priority", payload["priority"],
        ])
        created += 1
    return created


def provision(
    cli: PaperclipCLI,
    *,
    runtime_root: str | None = None,
    python_path: str | None = None,
) -> dict[str, Any]:
    blueprint = _load_blueprint()
    runtime_root = runtime_root or str(ROOT)
    python_path = python_path or str(ROOT / ".venv" / "bin" / "python")
    company = _resolve_company(cli, blueprint, create=True)
    created_here = bool(company.pop("_zhf_created_here", False))
    company_id = str(company["id"])

    current_agents = _find_list(cli.run(["agent", "list", "--company-id", company_id]), ("agents",))
    by_name = {_key(agent.get("name")): agent for agent in current_agents if agent.get("name")}
    agent_records: dict[str, dict[str, Any]] = {}
    created_agents = 0

    # The CEO is created first so every other agent can reference its stable id.
    definitions = list(blueprint["agents"])
    definitions.sort(key=lambda value: (0 if value.get("key") == "ceo" else 1, str(value.get("name", ""))))
    missing_names = [
        str(item.get("name") or "") for item in definitions
        if _key(item.get("name")) not in by_name
    ]
    if (
        missing_names
        and not created_here
        and company.get("requireBoardApprovalForNewAgents") is True
    ):
        raise ProvisionError(
            f"Company '{company.get('name')}' requires board approval for new agents. "
            f"Missing ZHF agents: {missing_names}. Approve/create them deliberately, then rerun setup; "
            "the existing company policy was not changed."
        )
    ceo_id = next((agent["id"] for agent in current_agents if _key(agent.get("name")) == "elara voss"), None)
    temporary_policy_change = created_here and bool(missing_names)

    try:
        if temporary_policy_change:
            # Set this before the CLI call so the finally block attempts to
            # restore the secure policy even if the update response is lost.
            cli.run([
                "company", "update", company_id,
                "--payload-json", json.dumps({"requireBoardApprovalForNewAgents": False}),
            ])
        for definition in definitions:
            name = str(definition.get("name") or "").strip()
            key = str(definition.get("key") or "default").lower()
            if not name:
                raise ProvisionError("Paperclip agent blueprint contains an empty name")
            record = by_name.get(_key(name))
            if record is None:
                payload = _agent_payload(
                    definition,
                    ceo_id=ceo_id,
                    runtime_root=runtime_root,
                    python_path=python_path,
                )
                created = cli.run([
                    "agent", "create",
                    "--company-id", company_id,
                    "--payload-json", json.dumps(payload, ensure_ascii=False),
                ])
                record = _find_object(created, ("agent",))
                if not record or not record.get("id"):
                    refreshed = _find_list(cli.run(["agent", "list", "--company-id", company_id]), ("agents",))
                    by_name = {_key(agent.get("name")): agent for agent in refreshed if agent.get("name")}
                    record = by_name.get(_key(name))
                if not record or not record.get("id"):
                    raise ProvisionError(f"Paperclip agent '{name}' was not present after create")
                by_name[_key(name)] = record
                created_agents += 1
            if key == "ceo":
                ceo_id = str(record["id"])
            agent_records[key] = record
    finally:
        if temporary_policy_change:
            # Permit the initial board-operated roster to be created directly,
            # then restore approval-on-hire even if a partial run needs repair.
            cli.run([
                "company", "update", company_id,
                "--payload-json", json.dumps({"requireBoardApprovalForNewAgents": True}),
            ])
            refreshed = _find_list(cli.run(["company", "list"]), ("companies",))
            restored = next((item for item in refreshed if str(item.get("id")) == company_id), None)
            if not restored or restored.get("requireBoardApprovalForNewAgents") is not True:
                raise ProvisionError("could not restore board approval for future Paperclip agent hires")

    skills, attachments = _ensure_skills(cli, company_id, runtime_root, agent_records)
    new_tasks = _ensure_tasks(cli, company_id, blueprint)

    # Re-read the three managed collections and validate the minimum expected
    # state instead of assuming successful write responses are sufficient.
    final_agents = _find_list(cli.run(["agent", "list", "--company-id", company_id]), ("agents",))
    final_skills = _find_list(cli.run(["skills", "list", "--company-id", company_id]), ("skills",))
    final_issues = _find_list(cli.run(["issue", "list", "--company-id", company_id]), ("issues", "tasks"))
    agent_names = {_key(item.get("name")) for item in final_agents}
    skill_names = {
        _key(item.get("slug") or item.get("key") or item.get("canonicalKey") or item.get("name")).split("/")[-1]
        for item in final_skills
    }
    issue_titles = {_key(item.get("title")) for item in final_issues}
    missing_agents = [a["name"] for a in definitions if _key(a.get("name")) not in agent_names]
    missing_skills = [s["metadata"]["name"] for s in _skill_sources() if _key(s["metadata"]["name"]) not in skill_names]
    missing_tasks = [task["title"] for task in blueprint["tasks"] if _key(task["title"]) not in issue_titles]
    if missing_agents or missing_skills or missing_tasks:
        raise ProvisionError(
            "post-provision validation failed: "
            f"agents missing={missing_agents}, skills missing={missing_skills}, tasks missing={missing_tasks}"
        )

    return {
        "company": {"id": company_id, "name": company.get("name", blueprint.get("company"))},
        "created_agents": created_agents,
        "agents": len(definitions),
        "skills": skills,
        "skill_attachments": attachments,
        "tasks_created": new_tasks,
        "tasks": len(blueprint["tasks"]),
        "heartbeats_enabled_by_setup": False,
        "live_trading_enabled_by_setup": False,
        "runtime_root": runtime_root,
    }


def _find_invite_url(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("url", "inviteUrl", "inviteURL", "shareUrl", "shareURL"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for value in payload.values():
            result = _find_invite_url(value)
            if result:
                return result
    elif isinstance(payload, list):
        for value in payload:
            result = _find_invite_url(value)
            if result:
                return result
    return None


def create_invite(cli: PaperclipCLI, role: str = "operator") -> dict[str, Any]:
    valid_roles = {"viewer", "operator", "admin", "owner"}
    if role not in valid_roles:
        raise ProvisionError(f"invalid invite role '{role}'. Choose one of: {', '.join(sorted(valid_roles))}")
    blueprint = _load_blueprint()
    company = _resolve_company(cli, blueprint, create=False)
    payload = cli.run([
        "invite", "create",
        "--company-id", str(company["id"]),
        "--payload-json", json.dumps({"role": role}),
    ])
    invite_url = _find_invite_url(payload)
    if not invite_url:
        # Never print the raw invite response here: it may contain a one-time
        # token. Keep the response only in memory and give a safe follow-up.
        return {
            "company": company.get("name", blueprint.get("company")),
            "role": role,
            "invite_created": True,
            "invite_url": None,
            "note": "Invite created; retrieve its one-time URL in Paperclip Settings → Members → Invites.",
        }
    return {
        "company": company.get("name", blueprint.get("company")),
        "role": role,
        "invite_created": True,
        "invite_url": invite_url,
        "note": "Single-use invite link. Share it privately; Paperclip does not email the recipient.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--action", choices=("provision", "invite", "status"), default="provision")
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL", DEFAULT_API_BASE))
    parser.add_argument("--docker-container", default=None, help="run the Paperclip CLI inside this container")
    parser.add_argument("--paperclip-bin", default=None, help="explicit native paperclipai executable")
    parser.add_argument("--runtime-root", default=None, help="repo path seen by agents (Docker: /workspace/zhf)")
    parser.add_argument("--python-path", default=None, help="Python interpreter available to process agents")
    parser.add_argument("--role", default="operator", help="human invite role: viewer, operator, admin, owner")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    try:
        cli = PaperclipCLI(
            api_base=args.api_base,
            docker_container=args.docker_container,
            binary=args.paperclip_bin,
            timeout=args.timeout,
        )
        if args.action == "status":
            result = {"healthy": cli.health(), "api_base": args.api_base.rstrip("/")}
            if not result["healthy"]:
                raise ProvisionError(f"Paperclip health check failed at {args.api_base}")
        elif args.action == "invite":
            result = create_invite(cli, args.role)
        else:
            result = provision(
                cli,
                runtime_root=args.runtime_root,
                python_path=args.python_path,
            )
    except ProvisionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if "401" in str(exc) or "403" in str(exc) or "unauthor" in str(exc).lower():
            print(
                "Sign in or pair the Paperclip CLI with a board account, then rerun this command. "
                "Authenticated Docker instances require completing first-user/board setup in the browser first.",
                file=sys.stderr,
            )
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.action == "invite":
            print(f"Invite created for role '{result['role']}' in {result['company']}.")
            if result.get("invite_url"):
                print("\nIMPORTANT: Single-use invite link; share privately:\n" + result["invite_url"])
            print(result["note"])
        else:
            print(
                f"ZHF Paperclip company {result['company']['name']} is ready: "
                f"{result['agents']} agents, {result['skills']} skills, "
                f"{result['tasks']} unassigned setup tasks."
            )
            print(f"New records this run: {result['created_agents']} agents, "
                  f"{result['tasks_created']} tasks. Heartbeats remain disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
