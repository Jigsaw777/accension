"""One daemon dispatcher, durable leases, and no stealing uncertain repository writers."""

import asyncio
import json

from ..contracts import ExecutionAborted
from ..safety import repo_lock
from .policy import CAPABILITIES, Principal, authorize
from .schema import ActorType, Mutation
from .service import CommandResult
from .store import WorkbenchError, now

SYSTEM = Principal("dispatcher", ActorType.SYSTEM, CAPABILITIES, frozenset({"*"}), True)


class Scheduler:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.service.store
        self.generation = runtime.agents.generation
        self.fence = None
        self.owner_lock = None
        self.loop_task = None
        self.workers = {}
        self.stopping = False
        self.error = None

    def acquire(self):
        directory = self.runtime.engine.settings.home / ".router" / "workbench-owner"
        directory.mkdir(parents=True, exist_ok=True)
        lock = repo_lock(directory)
        try:
            lock.__enter__()
        except ValueError:
            return False
        try:
            with self.store.router.transaction():
                row = self.store.db.execute("SELECT * FROM wb_leases WHERE resource_key='dispatcher'").fetchone()
                if row and row["expires_at"] > now() and row["owner_generation"] != self.generation:
                    lock.__exit__(None, None, None)
                    return False
                self.fence = (row["fence"] if row else 0) + 1
                self.store.db.execute(
                    "INSERT INTO wb_leases VALUES('dispatcher',?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET owner_generation=excluded.owner_generation,fence=excluded.fence,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                    (self.generation, self.fence, now() + 30000, now()),
                )
            self.owner_lock = lock
            return True
        except BaseException:
            lock.__exit__(None, None, None)
            raise

    def leader(self):
        row = self.store.db.execute("SELECT * FROM wb_leases WHERE resource_key='dispatcher'").fetchone()
        return bool(
            row
            and row["owner_generation"] == self.generation
            and row["fence"] == self.fence
            and row["expires_at"] > now()
            and not self.stopping
        )

    def heartbeat(self):
        with self.store.router.transaction():
            if not self.leader():
                raise ExecutionAborted("Dispatcher ownership expired")
            stamp = now()
            self.store.db.execute(
                "UPDATE wb_leases SET expires_at=?,updated_at=? WHERE owner_generation=? AND fence=?",
                (stamp + 30000, stamp, self.generation, self.fence),
            )
            for attempt_id, (_, fence) in self.workers.items():
                self.store.db.execute(
                    "UPDATE wb_attempts SET lease_until=? WHERE id=? AND owner_generation=? AND fence=? AND state IN ('CLAIMED','RUNNING','VERIFYING')",
                    (stamp + 30000, attempt_id, self.generation, fence),
                )

    def paused(self, workspace_id, project_id):
        return any(
            self.store.db.execute("SELECT value FROM metadata WHERE key=?", ("dispatch-paused:" + key,)).fetchone()
            for key in (workspace_id, project_id)
        )

    def claim(self):
        if self.stopping:
            return None
        with self.store.router.transaction():
            if not self.leader():
                return None
            rows = self.store.db.execute(
                "SELECT a.id FROM wb_attempts a JOIN wb_work_items w ON w.id=a.item_id WHERE a.state='QUEUED' ORDER BY w.priority,w.created_at,w.id LIMIT 100"
            ).fetchall()
            for candidate in rows:
                row, contract = self.runtime.execution.contract(candidate[0])
                item = self.store.get("work_item", contract.workspace_id, contract.item_id)
                if self.paused(contract.workspace_id, contract.project_id):
                    continue
                if contract.batch_id:
                    batch = self.store.db.execute(
                        "SELECT state FROM wb_delivery_batches WHERE id=?", (contract.batch_id,)
                    ).fetchone()
                    if batch and batch[0] == "PAUSED":
                        continue
                try:
                    if contract.batch_id:
                        self.runtime.delivery.authorization(contract.batch_id, contract.item_id)
                    ready, reason = self.runtime.service.readiness(item)
                    if ready != "READY":
                        raise WorkbenchError(reason or "NOT_READY", "Execution dependency or budget changed")
                    definition = self.runtime.agents.definition(contract.agent_definition_id)
                    instance = self.store.db.execute(
                        "SELECT * FROM wb_agent_instances WHERE id=(SELECT instance_id FROM wb_assignments WHERE id=?)",
                        (contract.assignment_id,),
                    ).fetchone()
                    if (
                        item.active_attempt_id != row["id"]
                        or not definition.enabled
                        or definition.revision != contract.agent_definition_revision
                        or self.runtime.agents.policy_hash(item) != contract.policy_revision_hash
                        or instance is None
                        or instance["expires_at"] <= now()
                        or instance["owner_generation"] != self.generation
                    ):
                        raise WorkbenchError("STALE_EXECUTION", "Execution contract or worker changed")
                    if contract.grant_id:
                        grant = self.store.db.execute(
                            "SELECT * FROM wb_execution_grants WHERE id=?", (contract.grant_id,)
                        ).fetchone()
                        if (
                            grant is None
                            or grant["revoked_at"] is not None
                            or grant["expires_at"] <= now()
                            or grant["consumed_attempt_id"] != row["id"]
                        ):
                            raise WorkbenchError("STALE_GRANT", "Execution grant expired or changed")
                    workspace = self.store.get("workspace", contract.workspace_id, contract.workspace_id)
                    from .policy import remaining_budget

                    reservation = self.store.db.execute(
                        "SELECT cap_microusd,state FROM wb_budget_reservations WHERE attempt_id=?", (row["id"],)
                    ).fetchone()
                    accounting = remaining_budget(
                        self.store, item, self.runtime.service.item_policy(item).budget_microusd
                    )
                    if (
                        reservation is None
                        or reservation[0] != contract.budget_microusd
                        or reservation[1] != "RESERVED"
                        or accounting["spent_microusd"] + accounting["held_microusd"]
                        > self.runtime.service.item_policy(item).budget_microusd
                    ):
                        raise WorkbenchError("BUDGET_EXHAUSTED", "Lifetime reservation no longer fits current budget")
                    active = self.store.db.execute(
                        "SELECT COUNT(*) FROM wb_attempts WHERE workspace_id=? AND state IN ('CLAIMED','RUNNING','VERIFYING')",
                        (contract.workspace_id,),
                    ).fetchone()[0]
                    if active >= min(
                        workspace.execution_policy.max_active_runs,
                        self.runtime.engine.settings.routing.max_parallel_workers,
                    ):
                        continue
                    repo_key = "repository:" + contract.repository_ref
                    lease = self.store.db.execute(
                        "SELECT * FROM wb_leases WHERE resource_key=?", (repo_key,)
                    ).fetchone()
                    if lease and lease["expires_at"] > now():
                        continue
                    # An expired reservation with an uncertain old attempt still forbids a new writer.
                    active_repo = self.store.db.execute(
                        "SELECT id,contract_json FROM wb_attempts WHERE state IN ('CLAIMED','RUNNING','VERIFYING','RECONCILING') AND id<>?",
                        (row["id"],),
                    ).fetchall()
                    if any(
                        json.loads(a["contract_json"]).get("repository_ref") == contract.repository_ref
                        for a in active_repo
                    ):
                        continue
                    fence = row["fence"] + 1
                    self.store.db.execute(
                        "UPDATE wb_attempts SET state='CLAIMED',owner_generation=?,fence=?,lease_until=? WHERE id=? AND state='QUEUED'",
                        (self.generation, fence, now() + 30000, row["id"]),
                    )
                    self.store.db.execute(
                        "INSERT INTO wb_leases VALUES(?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET owner_generation=excluded.owner_generation,fence=excluded.fence,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                        (repo_key, self.generation, self.fence, now() + 30000, now()),
                    )
                    self.store.db.execute(
                        "UPDATE wb_assignments SET status='ACTIVE' WHERE id=?", (contract.assignment_id,)
                    )
                    self.runtime.service.changed_item(item, {}, SYSTEM, "attempt_claimed")
                    return row["id"], fence
                except WorkbenchError as exc:
                    self.block(row["id"], item, exc.code)
            return None

    def block(self, attempt_id, item, code):
        self.store.db.execute(
            "UPDATE wb_attempts SET state='RECONCILING',fence=fence+1,error_code=? WHERE id=?", (code, attempt_id)
        )
        self.runtime.service.changed_item(
            item, {"status": "BLOCKED", "reason_code": "RECOVERY_REQUIRED"}, SYSTEM, "recovery_required"
        )

    def reconcile(self):
        with self.store.router.transaction():
            for row in self.store.db.execute(
                "SELECT id FROM wb_attempts WHERE state IN ('CLAIMED','RUNNING','VERIFYING') AND (owner_generation<>? OR lease_until<=?)",
                (self.generation, now()),
            ).fetchall():
                attempt, contract = self.runtime.execution.contract(row[0])
                item = self.store.get("work_item", contract.workspace_id, contract.item_id)
                self.block(attempt["id"], item, "OWNER_UNCERTAIN")
        for row in self.store.db.execute("SELECT id FROM wb_attempts WHERE state='RECONCILING'").fetchall():
            try:
                self.runtime.execution.reconcile_terminal(row[0])
            except (ValueError, OSError, WorkbenchError):
                pass  # Lock or evidence uncertainty retains the active attempt.

    def cancel_owned(self):
        for attempt_id, (task, fence) in list(self.workers.items()):
            row = self.store.db.execute(
                "SELECT cancel_requested_at,owner_generation,fence FROM wb_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if (
                row
                and row["cancel_requested_at"]
                and row["owner_generation"] == self.generation
                and row["fence"] == fence
            ):
                if not task.cancelling():
                    task.cancel()
        with self.store.router.transaction():
            for row in self.store.db.execute(
                "SELECT id FROM wb_attempts WHERE state='QUEUED' AND cancel_requested_at IS NOT NULL"
            ).fetchall():
                attempt, contract = self.runtime.execution.contract(row[0])
                if self.store.db.execute("SELECT 1 FROM runs WHERE id=?", (contract.plan_id,)).fetchone():
                    self.block(
                        attempt["id"],
                        self.store.get("work_item", contract.workspace_id, contract.item_id),
                        "INTENT_UNCERTAIN",
                    )
                    continue
                self.store.db.execute(
                    "UPDATE wb_attempts SET state='CANCELLED',finished_at=? WHERE id=?", (now(), attempt["id"])
                )
                self.store.db.execute(
                    "UPDATE wb_assignments SET status='RELEASED',ended_at=? WHERE id=?", (now(), contract.assignment_id)
                )
                self.store.db.execute(
                    "UPDATE wb_budget_reservations SET state='SETTLED',settled_at=? WHERE attempt_id=?",
                    (now(), attempt["id"]),
                )
                self.runtime.service.changed_item(
                    self.store.get("work_item", contract.workspace_id, contract.item_id),
                    {"status": "CANCELLED", "active_attempt_id": None, "reason_code": None},
                    SYSTEM,
                    "cleanup_acknowledged",
                )
                from .metrics import reconcile_attempt

                reconcile_attempt(self.runtime, attempt["id"])

    async def work(self, attempt_id, fence):
        try:
            await self.runtime.execution.execute(attempt_id, self.generation, fence)
        except BaseException as exc:
            self.error = type(exc).__name__
            from .connectors import ConnectorUncertain

            cleaned = (
                False
                if isinstance(exc, ConnectorUncertain)
                else self.runtime.execution.terminal_failure(attempt_id, self.generation, fence)
            )
            with self.store.router.transaction():
                row, contract = self.runtime.execution.contract(attempt_id)
                if row["owner_generation"] == self.generation and row["fence"] == fence:
                    if not cleaned:
                        self.block(
                            attempt_id,
                            self.store.get("work_item", contract.workspace_id, contract.item_id),
                            type(exc).__name__,
                        )
        finally:
            with self.store.router.transaction():
                _, contract = self.runtime.execution.contract(attempt_id)
                self.store.db.execute(
                    "DELETE FROM wb_leases WHERE resource_key=? AND owner_generation=? AND fence=?",
                    ("repository:" + contract.repository_ref, self.generation, self.fence),
                )
            self.workers.pop(attempt_id, None)

    async def loop(self):
        try:
            while not self.stopping:
                self.heartbeat()
                self.cancel_owned()
                self.reconcile()
                self.runtime.delivery.tick()
                while (claim := self.claim()) is not None:
                    task = asyncio.create_task(self.work(*claim))
                    self.workers[claim[0]] = (task, claim[1])
                self.runtime.service.wake.changed.clear()
                await asyncio.sleep(1)
        except ExecutionAborted as exc:
            self.error = str(exc)
            for task, _ in list(self.workers.values()):
                task.cancel()
        except Exception as exc:
            self.error = type(exc).__name__
            self.stopping = True
            for task, _ in list(self.workers.values()):
                task.cancel()

    async def start(self):
        if not self.acquire():
            self.error = "Another daemon owns Workbench dispatch"
            return
        self.reconcile()
        self.loop_task = asyncio.create_task(self.loop())

    async def close(self):
        self.stopping = True
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        tasks = [t for t, _ in self.workers.values()]
        with self.store.router.transaction():
            for attempt_id in self.workers:
                self.store.db.execute(
                    "UPDATE wb_attempts SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE id=? AND owner_generation=?",
                    (now(), attempt_id, self.generation),
                )
                _, contract = self.runtime.execution.contract(attempt_id)
                self.runtime.service.changed_item(
                    self.store.get("work_item", contract.workspace_id, contract.item_id),
                    {},
                    SYSTEM,
                    "shutdown_cancel_requested",
                )
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10)
            if pending:
                with self.store.router.transaction():
                    for attempt_id, (task, fence) in self.workers.items():
                        if task in pending:
                            row, contract = self.runtime.execution.contract(attempt_id)
                            if row["fence"] == fence:
                                self.block(
                                    attempt_id,
                                    self.store.get("work_item", contract.workspace_id, contract.item_id),
                                    "CLEANUP_TIMEOUT",
                                )
                self.error = "CLEANUP_TIMEOUT"
                raise WorkbenchError(
                    "CLEANUP_TIMEOUT", "Workers still own cleanup; dispatcher lock and database must remain open", 503
                )
        with self.store.router.transaction():
            self.store.db.execute(
                "UPDATE wb_leases SET expires_at=? WHERE resource_key='dispatcher' AND owner_generation=? AND fence=?",
                (now(), self.generation, self.fence),
            )
        if self.owner_lock:
            self.owner_lock.__exit__(None, None, None)
            self.owner_lock = None

    def dispatch(self, kind, workspace_id, entity_id, request, principal):
        from typing import Literal

        from ..schema import Strict

        class Dispatch(Mutation, Strict):
            action: Literal["pause", "resume"]

        body = Dispatch.model_validate(request)
        authorize(principal, workspace_id, "execution.request")

        def apply():
            entity = self.store.get(kind, workspace_id, entity_id)
            if entity.revision != body.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Dispatch scope changed")
            key = "dispatch-paused:" + entity_id
            if body.action == "pause":
                self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,'paused')", (key,))
            else:
                self.store.db.execute("DELETE FROM metadata WHERE key=?", (key,))
            updated = type(entity).model_validate(
                {
                    **entity.model_dump(),
                    "revision": entity.revision + 1,
                    "updated_at": now(),
                    "updated_by": principal.id,
                }
            )
            self.runtime.service.persist(kind, updated, principal, "dispatch_" + body.action, previous=entity)
            return CommandResult(200, {"id": entity_id, "revision": updated.revision, "paused": body.action == "pause"})

        return self.runtime.service.command(
            principal,
            workspace_id,
            "execution.request",
            body.command_id,
            "dispatch",
            {"entity_id": entity_id, **body.model_dump(mode="json")},
            apply,
        )
