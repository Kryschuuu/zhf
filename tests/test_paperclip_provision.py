from __future__ import annotations

import json
from pathlib import Path

from scripts import paperclip_provision as provisioner


class FakePaperclip:
    def __init__(self):
        self.company = {
            "id": "company-1",
            "name": "zhf-trading",
            "requireBoardApprovalForNewAgents": False,
        }
        self.company_exists = True
        self.agents = []
        self.skills = []
        self.issues = []
        self.attachments = set()
        self.next_id = 1
        self.calls = []

    def _id(self, prefix):
        value = f"{prefix}-{self.next_id}"
        self.next_id += 1
        return value

    @staticmethod
    def _option(args, name, default=None):
        try:
            return args[args.index(name) + 1]
        except (ValueError, IndexError):
            return default

    def run(self, args, *, allow_failure=False):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["company", "list"]:
            return {"companies": [self.company] if self.company_exists else []}
        if args[:2] == ["company", "create"]:
            payload = json.loads(self._option(args, "--payload-json", "{}"))
            self.company = {
                **payload,
                "id": "company-new",
                "requireBoardApprovalForNewAgents": True,
            }
            self.company_exists = True
            return {"company": self.company}
        if args[:2] == ["company", "update"]:
            payload = json.loads(self._option(args, "--payload-json", "{}"))
            self.company.update(payload)
            return {"company": self.company}
        if args[:2] == ["agent", "list"]:
            return {"agents": list(self.agents)}
        if args[:2] == ["agent", "create"]:
            payload = json.loads(self._option(args, "--payload-json", "{}"))
            record = {**payload, "id": self._id("agent"), "status": "idle"}
            self.agents.append(record)
            return {"agent": record}
        if args[:2] == ["skills", "list"]:
            return {"skills": list(self.skills)}
        if args[:2] == ["skills", "create"]:
            slug = self._option(args, "--slug")
            body_path = Path(self._option(args, "--body-file"))
            assert body_path.is_file()
            record = {
                "id": self._id("skill"),
                "slug": slug,
                "name": self._option(args, "--name"),
                "description": self._option(args, "--description"),
            }
            self.skills.append(record)
            return {"skill": record}
        if args[:3] == ["skills", "agent", "sync"]:
            agent_id = args[3]
            skill = self._option(args, "--skill")
            self.attachments.add((agent_id, skill))
            return {"synced": True}
        if args[:2] == ["issue", "list"]:
            return {"issues": list(self.issues)}
        if args[:2] == ["issue", "create"]:
            issue = {
                "id": self._id("issue"),
                "title": self._option(args, "--title"),
                "description": self._option(args, "--description"),
                "status": self._option(args, "--status"),
                "priority": self._option(args, "--priority"),
            }
            self.issues.append(issue)
            return {"issue": issue}
        raise AssertionError(f"unexpected Paperclip CLI command: {args}")


def test_paperclip_provision_is_idempotent_and_keeps_agents_paused(monkeypatch, tmp_path):
    fake = FakePaperclip()
    monkeypatch.setattr(provisioner, "GENERATED_SKILLS", tmp_path / "generated-skills")
    monkeypatch.setattr(provisioner, "select_agent_model", lambda agent: f"opencode/{agent}-model")

    first = provisioner.provision(fake, runtime_root=str(provisioner.ROOT), python_path=str(provisioner.ROOT / ".venv" / "bin" / "python"))
    second = provisioner.provision(fake, runtime_root=str(provisioner.ROOT), python_path=str(provisioner.ROOT / ".venv" / "bin" / "python"))

    assert first["created_agents"] == 7
    assert first["tasks_created"] == 4
    assert first["agents"] == 7
    assert first["skills"] == 2
    assert first["skill_attachments"] == 2
    assert first["heartbeats_enabled_by_setup"] is False
    assert first["live_trading_enabled_by_setup"] is False

    assert second["created_agents"] == 0
    assert second["tasks_created"] == 0
    assert second["agents"] == 7
    assert len(fake.agents) == 7
    assert len(fake.skills) == 2
    assert len(fake.issues) == 4
    assert len(fake.attachments) == 2
    assert all(agent["runtimeConfig"]["heartbeat"]["enabled"] is False for agent in fake.agents)
    assert all(issue["status"] == "backlog" for issue in fake.issues)
    assert all("assigneeAgentId" not in issue for issue in fake.issues)


def test_new_company_restores_future_agent_approval_policy(monkeypatch, tmp_path):
    fake = FakePaperclip()
    fake.company_exists = False
    monkeypatch.setattr(provisioner, "GENERATED_SKILLS", tmp_path / "generated-skills")
    monkeypatch.setattr(provisioner, "select_agent_model", lambda agent: f"opencode/{agent}-model")

    result = provisioner.provision(fake)

    assert result["agents"] == 7
    assert fake.company["requireBoardApprovalForNewAgents"] is True
    updates = [
        json.loads(args[args.index("--payload-json") + 1])
        for args in fake.calls if args[:2] == ["company", "update"]
    ]
    assert updates == [
        {"requireBoardApprovalForNewAgents": False},
        {"requireBoardApprovalForNewAgents": True},
    ]


def test_new_company_policy_is_restored_when_roster_creation_fails(monkeypatch):
    class FailOnAgentCreate(FakePaperclip):
        def run(self, args, *, allow_failure=False):
            if list(args)[:2] == ["agent", "create"]:
                raise provisioner.ProvisionError("simulated agent-create failure")
            return super().run(args, allow_failure=allow_failure)

    fake = FailOnAgentCreate()
    fake.company_exists = False
    with __import__("pytest").raises(provisioner.ProvisionError, match="simulated agent-create failure"):
        provisioner.provision(fake)

    assert fake.company["requireBoardApprovalForNewAgents"] is True
    updates = [
        json.loads(args[args.index("--payload-json") + 1])
        for args in fake.calls if args[:2] == ["company", "update"]
    ]
    assert updates == [
        {"requireBoardApprovalForNewAgents": False},
        {"requireBoardApprovalForNewAgents": True},
    ]


def test_skill_slug_cannot_escape_generated_skill_directory(monkeypatch, tmp_path):
    skill = tmp_path / "agents" / "demo" / "skills" / "bad.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: ../secret\ndescription: invalid traversal\nagent: ceo\n---\nBody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provisioner, "ROOT", tmp_path)

    with __import__("pytest").raises(provisioner.ProvisionError, match="unsafe/invalid slug"):
        provisioner._skill_sources()


def test_prompt_path_cannot_escape_repository(monkeypatch):
    monkeypatch.setattr(provisioner, "select_agent_model", lambda _agent: "opencode/big-pickle")
    blueprint = provisioner._load_blueprint()
    ceo = next(agent for agent in blueprint["agents"] if agent["key"] == "ceo")
    ceo["adapter_config"]["system_prompt_file"] = "../../etc/passwd"

    with __import__("pytest").raises(provisioner.ProvisionError, match="must be inside the ZHF repository"):
        provisioner._agent_payload(
            ceo,
            ceo_id=None,
            runtime_root=str(provisioner.ROOT),
            python_path=str(provisioner.ROOT / ".venv" / "bin" / "python"),
        )


def test_opencode_agent_fails_closed_when_catalog_has_no_available_model(monkeypatch):
    monkeypatch.setattr(provisioner, "select_agent_model", lambda _agent: None)
    blueprint = provisioner._load_blueprint()
    ceo = next(agent for agent in blueprint["agents"] if agent["key"] == "ceo")

    with __import__("pytest").raises(provisioner.ProvisionError, match="no available OpenCode model"):
        provisioner._agent_payload(
            ceo,
            ceo_id=None,
            runtime_root=str(provisioner.ROOT),
            python_path=str(provisioner.ROOT / ".venv" / "bin" / "python"),
        )


def test_process_agent_paths_and_arguments_are_container_ready(monkeypatch):
    monkeypatch.setattr(provisioner, "select_agent_model", lambda _agent: "opencode/gpt-5-nano")
    blueprint = provisioner._load_blueprint()
    risk = next(agent for agent in blueprint["agents"] if agent["key"] == "risk")
    payload = provisioner._agent_payload(
        risk,
        ceo_id="agent-ceo",
        runtime_root="/workspace/zhf",
        python_path="/opt/zhf-venv/bin/python",
    )
    assert payload["adapterType"] == "process"
    assert payload["adapterConfig"]["command"] == "/opt/zhf-venv/bin/python"
    assert payload["adapterConfig"]["args"] == ["-m", "scripts.risk.run"]
    assert payload["adapterConfig"]["cwd"] == "/workspace/zhf"
    assert payload["adapterConfig"]["env"]["OPENCODE_MODEL_RISK"] == "opencode/gpt-5-nano"


def test_opencode_prompt_path_uses_runtime_mount(monkeypatch):
    monkeypatch.setattr(provisioner, "select_agent_model", lambda _agent: "opencode/big-pickle")
    blueprint = provisioner._load_blueprint()
    ceo = next(agent for agent in blueprint["agents"] if agent["key"] == "ceo")
    payload = provisioner._agent_payload(
        ceo,
        ceo_id=None,
        runtime_root="/workspace/zhf",
        python_path="/opt/zhf-venv/bin/python",
    )
    assert payload["adapterConfig"]["cwd"] == "/workspace/zhf"
    assert payload["adapterConfig"]["instructionsFilePath"] == "/workspace/zhf/prompts/ceo.md"
    assert payload["adapterConfig"]["model"] == "opencode/big-pickle"
    assert payload["runtimeConfig"]["heartbeat"]["enabled"] is False


def test_company_approval_policy_stops_direct_provisioning(monkeypatch):
    fake = FakePaperclip()
    fake.company["requireBoardApprovalForNewAgents"] = True
    with __import__("pytest").raises(provisioner.ProvisionError, match="requires board approval"):
        provisioner.provision(fake)
    assert fake.agents == []
