"""Disposable three-item mock Workbench workflow with actual Engine/checks/receipts."""

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from local_ai_router.config import Repository, load
from local_ai_router.engine import Engine
from local_ai_router.schema import Request
from local_ai_router.workbench.policy import CAPABILITIES, Principal
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import identity

HUMAN = Principal("demo-fixture-operator", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)


async def demo(home, root, verify_clients=None):
    root.mkdir()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True, **options)
    (root / "test_contract.py").write_text(
        "import unittest\nfrom greeting import greet\nclass Contract(unittest.TestCase):\n"
        '    def test_named(self): self.assertEqual(greet(" Ada "), "Hello, Ada!")\n'
        '    def test_blank(self): self.assertEqual(greet(" "), "Hello, world!")\n'
    )
    settings = load(home, mock=True)
    settings.discovery.enabled = False
    settings.repositories = [
        Repository(path=str(root), validation={"tests": ["{python}", "-m", "unittest", "discover", "-v"]})
    ]
    engine = Engine(settings)
    runtime = WorkbenchRuntime(engine)
    try:
        ws = runtime.service.seed_defaults()
        if verify_clients:
            runtime.service.update(
                "workspace",
                ws.id,
                ws.id,
                {"command_id": identity(), "expected_revision": ws.revision, "privacy_mode": "PRIVATE_LAN"},
                HUMAN,
            )
        reference = next(iter(runtime.service.repositories()))
        project = runtime.service.create(
            "project",
            {
                "command_id": identity(),
                "workspace_id": ws.id,
                "key": "DEMO",
                "name": "Greeting demo",
                "repository_ref": reference,
            },
            HUMAN,
        ).body
        prd = runtime.service.create(
            "prd",
            {
                "command_id": identity(),
                "workspace_id": ws.id,
                "project_id": project["id"],
                "title": "Greeting function, tests and documentation",
                "spec": {
                    "problem_statement": "Greeting behavior needs a reusable implementation",
                    "objectives": ["Greeting verified"],
                    "scope": ["Greeting, tests, usage"],
                    "functional_requirements": [{"id": "R1", "text": "Implement greet and its documentation/tests"}],
                    "acceptance_criteria": [{"id": "A1", "text": "Named and blank input verified"}],
                },
            },
            HUMAN,
        ).body
        for command in ("submit_review", "approve"):
            prd = runtime.service.prd_command(
                ws.id,
                prd["id"],
                {"command_id": identity(), "expected_revision": prd["revision"], "command": command},
                HUMAN,
            ).body
        plan = await engine.plan(
            Request(
                task="Build greeting function, tests and usage documentation",
                repo_path=str(root),
                execution_mode="plan-only",
            )
        )
        nodes = {node.id: node for node in plan.tasks}
        nodes["docs"].dependencies = ["tests"]
        plan.tasks = [nodes["greeting"], nodes["tests"], nodes["docs"]]
        staged = runtime.planning.preview(
            {
                "command_id": identity(),
                "workspace_id": ws.id,
                "project_id": project["id"],
                "prd_id": prd["id"],
                "prd_revision": prd["revision"],
                "plans": [plan.model_dump(mode="json")],
                "allowed_paths": ["greeting.py", "test_greeting.py", "USAGE.md"],
                "requirement_groups": [["R1"]],
            },
            HUMAN,
        ).body
        imported = runtime.planning.commit(
            ws.id,
            staged["id"],
            {
                "command_id": identity(),
                "expected_revision": staged["revision"],
                "source_hash": staged["source_hash"],
                "actions": {proposal["proposal_id"]: "create" for proposal in staged["proposal"]["items"]},
            },
            HUMAN,
        ).body
        ids = [imported["items"]["b0_" + key] for key in ("greeting", "tests", "docs")]
        blocked = runtime.service.store.get("work_item", ws.id, ids[1])
        assert runtime.service.readiness(blocked) == ("BLOCKED", "DEPENDENCY_NOT_DONE")
        runtime.agents.configure(
            {
                "command_id": identity(),
                "name": "Fixture engine",
                "allowed_workspaces": [ws.id],
                "repository_permissions": [reference],
            },
            HUMAN,
        )
        await runtime.start(dispatcher=True)
        attempts = []
        for item_id in ids:
            item = runtime.service.store.get("work_item", ws.id, item_id)
            runtime.service.item_command(
                ws.id,
                item_id,
                {"command_id": identity(), "expected_revision": item.revision, "command": "ready"},
                HUMAN,
            )
            decision = runtime.agents.preview(ws.id, item_id, HUMAN)
            assigned = runtime.agents.assign(
                ws.id,
                item_id,
                {
                    "command_id": identity(),
                    "expected_revision": decision["item_revision"],
                    "agent_id": decision["selected"],
                    "policy_revision_hash": decision["policy_revision_hash"],
                },
                HUMAN,
            ).body["item"]
            preflight = runtime.agents.execution_preflight(ws.id, item_id, HUMAN)
            granted = runtime.service.item_command(
                ws.id,
                item_id,
                {
                    "command_id": identity(),
                    "expected_revision": assigned["revision"],
                    "command": "approve_execution",
                    "contract_revision": assigned["contract_revision"],
                    "contract_hash": preflight["contract_hash"],
                    "policy_revision_hash": preflight["policy_revision_hash"],
                    "reason": "Disposable fixture contract approved by scripted operator",
                },
                HUMAN,
            ).body["item"]
            command = {
                "command_id": identity(),
                "expected_revision": granted["revision"],
                "command": "request_execution",
            }
            accepted = runtime.service.item_command(ws.id, item_id, command, HUMAN)
            assert runtime.service.item_command(ws.id, item_id, command, HUMAN).body == accepted.body
            attempt = accepted.body["attempt_id"]
            for _ in range(200):
                current = runtime.service.store.get("work_item", ws.id, item_id)
                if current.status in {"REVIEW", "FAILED", "BLOCKED"}:
                    break
                await asyncio.sleep(0.05)
            assert current.status == "REVIEW", runtime.execution.read_attempt(ws.id, attempt, HUMAN)
            evidence, data = runtime.service.evidence(current)
            assert data["checks"] and all(c["passed"] for c in data["checks"])
            runtime.service.item_command(
                ws.id,
                item_id,
                {
                    "command_id": identity(),
                    "expected_revision": current.revision,
                    "command": "approve_completion",
                    "contract_revision": current.contract_revision,
                    "evidence_hash": evidence["evidence_hash"],
                    "reason": "Fixture operator inspected executed tests and artifact hashes",
                },
                HUMAN,
            )
            attempts.append(attempt)
        keys = [runtime.service.store.get("work_item", ws.id, i).display_key for i in ids]
        if verify_clients:
            await verify_clients(runtime, "completed")
        assert keys == ["DEMO-1", "DEMO-2", "DEMO-3"]
        revisions = [runtime.service.store.get("work_item", ws.id, i).revision for i in ids]
        snapshots = [runtime.execution.read_attempt(ws.id, a, HUMAN, "receipt") for a in attempts]
        assert len({s["source_run_id"] for s in snapshots}) == 3
        stages = [
            r[0] for r in engine.store.db.execute("SELECT event_type FROM wb_events WHERE entity_type='work_item'")
        ]
        assert all(
            stage in stages
            for stage in (
                "engine_execution_start",
                "engine_validation_start",
                "verification_ready",
                "completion_approve",
            )
        )
        await runtime.close()
        await engine.close()
        engine = Engine(settings)
        runtime = WorkbenchRuntime(engine)
        assert [runtime.service.store.get("work_item", ws.id, i).revision for i in ids] == revisions
        assert [runtime.execution.read_attempt(ws.id, a, HUMAN, "receipt") for a in attempts] == snapshots
        assert all(runtime.service.store.get("work_item", ws.id, i).status == "DONE" for i in ids)
        if verify_clients:
            await verify_clients(runtime, "restarted")
        return {
            "fixture": "mock greeting; scripted human operator",
            "keys": keys,
            "attempts": len(attempts),
            "checks": "executed unittest",
            "restart": "keys/revisions/receipts preserved",
            "paid_inference": False,
            "files": [p.name for p in root.iterdir() if p.is_file()],
        }
    finally:
        await runtime.close()
        await engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, help="Optional directory for disposable fixture")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="accension-workbench-demo-", dir=args.parent) as directory:
        base = Path(directory)
        print(json.dumps(asyncio.run(demo(base / "home", base / "repo")), indent=2))


if __name__ == "__main__":
    main()
