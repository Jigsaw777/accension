"""Durable human-approved coordination over existing Work Items and attempts."""

import json
from decimal import ROUND_CEILING, Decimal

from .events import append_event
from .model_options import inventory_hash, validate_bindings
from .policy import authorize
from .schema import (
    DeliveryCommand,
    DeliveryConfirm,
    DeliveryPreview,
    DeliveryProjection,
    DeliveryReapprove,
    budget_microusd,
)
from .service import CommandResult, payload_hash
from .store import WorkbenchError, identity, now


class Delivery:
    def __init__(self, runtime):
        self.runtime, self.service = runtime, runtime.service
        self.store = self.service.store

    def list(self, workspace_id, principal, *, project_id=None, cursor=None, limit=50):
        authorize(principal, workspace_id, "workspace.read")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise WorkbenchError("INVALID_PAGE", "Delivery page limit must be 1..50", 422)
        scope = {"kind": "delivery_list", "workspace": workspace_id, "project": project_id, "principal": principal.id}
        after = ""
        if cursor:
            position = self.runtime.journal.decode(cursor)
            if (
                not isinstance(position, dict)
                or any(position.get(k) != v for k, v in scope.items())
                or not isinstance(position.get("after"), str)
                or type(position.get("expires")) is not int
                or position["expires"] <= now()
            ):
                raise WorkbenchError("CURSOR_QUERY_MISMATCH", "Delivery list scope changed", 422)
            after = position["after"]
        with self.store.router.lock:
            self.store.get("workspace", workspace_id, workspace_id)
            if project_id:
                self.store.get("project", workspace_id, project_id)
            rows = self.store.db.execute(
                "SELECT id,project_id,prd_id,state,revision,updated_at FROM wb_delivery_batches WHERE workspace_id=? AND id>?"
                + (" AND project_id=?" if project_id else "")
                + " ORDER BY id LIMIT ?",
                [workspace_id, after, *([project_id] if project_id else []), limit + 1],
            ).fetchall()
            items = [dict(r) for r in rows[:limit]]
            return {
                "items": items,
                "has_more": len(rows) > limit,
                "next_cursor": self.runtime.journal.token(
                    {**scope, "after": items[-1]["id"], "expires": now() + 900000}
                )
                if len(rows) > limit
                else None,
            }

    def event(self, batch_id, principal, event_type):
        row = self.store.db.execute("SELECT * FROM wb_delivery_batches WHERE id=?", (batch_id,)).fetchone()
        append_event(
            self.store,
            "delivery",
            DeliveryProjection.model_validate({key: row[key] for key in DeliveryProjection.model_fields}),
            principal,
            event_type,
        )

    def preview(self, request, principal):
        body = DeliveryPreview.model_validate(request)
        authorize(principal, body.workspace_id, "workspace.read")
        with self.store.router.lock:
            imported = self.store.get("plan_import", body.workspace_id, body.import_id)
            prd = self.store.get("prd", body.workspace_id, imported.prd_id)
            if (
                imported.project_id != body.project_id
                or imported.status != "COMMITTED"
                or prd.status != "APPROVED"
                or prd.revision != imported.prd_revision
            ):
                raise WorkbenchError("STALE_SOURCE", "Delivery requires the current approved imported PRD", 422)
            if not now() < body.expires_at <= now() + 86_400_000:
                raise WorkbenchError("INVALID_EXPIRY", "Delivery grant expires within one day", 422)
            members, blockers = [], []
            for item_id in sorted(body.item_ids):
                item = self.store.get("work_item", body.workspace_id, item_id)
                if (
                    item.project_id != body.project_id
                    or item.prd_id != prd.id
                    or item.contract_revision != body.contract_revisions[item.id]
                    or item.active_attempt_id
                    or item.status not in {"BACKLOG", "READY"}
                ):
                    raise WorkbenchError("STALE_CONTRACT", "Delivery selection changed", 422)
                if not self.store.db.execute(
                    "SELECT 1 FROM wb_plan_bindings WHERE import_id=? AND work_item_id=?", (body.import_id, item_id)
                ).fetchone():
                    raise WorkbenchError("INVALID_SOURCE", "Ticket must belong to the selected import", 422)
                agent = self.runtime.agents.definition(body.agents[item_id])
                project = self.store.get("project", body.workspace_id, body.project_id)
                if (
                    not agent.enabled
                    or body.workspace_id not in agent.allowed_workspaces
                    or (agent.allowed_projects and body.project_id not in agent.allowed_projects)
                    or project.repository_ref not in agent.repository_permissions
                ):
                    raise WorkbenchError("AGENT_INELIGIBLE", "Selected delivery agent scope denied", 422)
                self.service.readiness(item)
                validate_bindings(self.runtime, agent, principal)
                policy = self.service.item_policy(item)
                if (
                    not set(item.spec.required_capabilities) <= set(agent.capabilities)
                    or not set(item.spec.required_tools) <= set(agent.tools)
                    or not set(item.spec.required_skills) <= set(agent.skills)
                ):
                    raise WorkbenchError(
                        "AGENT_INELIGIBLE", "Selected agent lacks required capabilities, tools or skills", 422
                    )
                dependencies = [
                    row[0]
                    for row in self.store.db.execute(
                        "SELECT blocker_id FROM wb_dependencies WHERE workspace_id=? AND dependent_id=? ORDER BY blocker_id",
                        (body.workspace_id, item_id),
                    )
                ]
                for dependency in dependencies:
                    upstream = self.store.get("work_item", body.workspace_id, dependency)
                    if dependency not in body.item_ids and upstream.status != "DONE":
                        blockers.append(
                            {"item_id": item_id, "dependency_id": dependency, "reason": "EXTERNAL_DEPENDENCY"}
                        )
                members.append(
                    {
                        "item_id": item_id,
                        "contract_revision": item.contract_revision,
                        "contract_hash": self.runtime.agents.policy_hash(item),
                        "agent_id": agent.id,
                        "agent_revision": agent.revision,
                        "model_bindings": agent.model_bindings.model_dump(mode="json"),
                        "individual_cap": policy.budget_microusd,
                        "dependencies": dependencies,
                        "paths": item.spec.allowed_paths,
                        "checks": item.spec.validation_checks,
                    }
                )
            data = {
                "input": body.model_dump(mode="json"),
                "members": members,
                "principal_id": principal.id,
                "device_credential_id": getattr(principal, "credential_id", None),
                "device_id": getattr(principal, "device_id", None),
                "device_browser": getattr(principal, "browser", False),
                "import_hash": imported.source_hash,
                "prd_id": prd.id,
                "prd_revision": prd.revision,
                "model_policy_hash": inventory_hash(self.runtime.engine.settings),
                "blockers": blockers,
            }
            data["input"]["item_ids"] = sorted(body.item_ids)
            if budget_microusd(body.budget) > sum(member["individual_cap"] for member in members):
                raise WorkbenchError("INVALID_BUDGET", "Batch cap cannot exceed selected item allowances", 422)
            digest = payload_hash(data)
            return {
                "preview": self.runtime.journal.token(
                    {
                        "kind": "delivery-preview",
                        "version": 1,
                        "input": data["input"],
                        "principal_id": principal.id,
                        "hash": digest,
                    }
                ),
                "selection_hash": digest,
                "scope": data,
                "blockers": blockers,
                "inference": False,
            }

    def confirm(self, request, principal):
        body = DeliveryConfirm.model_validate(request)
        authorize(principal, body.workspace_id, "execution.approve")
        if principal.actor_type != "HUMAN":
            raise WorkbenchError("HUMAN_REQUIRED", "Delivery approval requires an observed human", 403)

        def apply():
            signed = self.runtime.journal.decode(body.preview, max_length=500000)
            if (
                not isinstance(signed, dict)
                or signed.get("kind") != "delivery-preview"
                or signed.get("version") != 1
                or signed.get("hash") != body.selection_hash
            ):
                raise WorkbenchError("INVALID_PREVIEW", "Invalid delivery preview", 422)
            if signed["principal_id"] != principal.id or signed["input"]["workspace_id"] != body.workspace_id:
                raise WorkbenchError("INVALID_PREVIEW", "Delivery principal or scope changed", 422)
            refreshed = self.preview(signed["input"], principal)
            if refreshed["selection_hash"] != body.selection_hash or refreshed["blockers"]:
                raise WorkbenchError(
                    "STALE_PREVIEW", "Delivery preview changed or has unresolved external dependencies", 422
                )
            data = refreshed["scope"]
            batch_id, stamp = identity(), now()
            fields = {
                "id": batch_id,
                "workspace_id": body.workspace_id,
                "project_id": data["input"]["project_id"],
                "prd_id": data["prd_id"],
                "prd_revision": data["prd_revision"],
                "import_id": data["input"]["import_id"],
                "import_hash": data["import_hash"],
                "state": "APPROVED",
                "revision": 1,
                "approved_by": principal.id,
                "actor_type": "HUMAN",
                "approved_at": stamp,
                "expires_at": DeliveryPreview.model_validate(data["input"]).expires_at,
                "selection_hash": body.selection_hash,
                "policy_hash": data["model_policy_hash"],
                "max_cost_microusd": budget_microusd(data["input"]["budget"]),
                "created_at": stamp,
                "updated_at": stamp,
            }
            self.store.db.execute(
                "INSERT INTO wb_delivery_batches("
                + ",".join(fields)
                + ") VALUES("
                + ",".join("?" for _ in fields)
                + ")",
                tuple(fields.values()),
            )
            for member in data["members"]:
                self.store.db.execute(
                    "INSERT INTO wb_delivery_members(batch_id,item_id,approved_contract_revision,approved_contract_hash,allowed_agent_revisions_json,model_policy_hash,member_state) VALUES(?,?,?,?,?,?,'pending')",
                    (
                        batch_id,
                        member["item_id"],
                        member["contract_revision"],
                        member["contract_hash"],
                        json.dumps({member["agent_id"]: member["agent_revision"]}),
                        data["model_policy_hash"],
                    ),
                )
            grant_id = identity()
            self.store.db.execute(
                "INSERT INTO wb_delivery_grants(id,batch_id,principal_id,device_credential_id,scope_hash,expires_at) VALUES(?,?,?,?,?,?)",
                (
                    grant_id,
                    batch_id,
                    principal.id,
                    data["device_credential_id"] if not data["device_browser"] else None,
                    body.selection_hash,
                    fields["expires_at"],
                ),
            )
            if data["device_id"]:
                self.store.db.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    (
                        "delivery-origin:" + grant_id,
                        json.dumps(
                            {
                                "device_id": data["device_id"],
                                "credential_id": data["device_credential_id"],
                                "browser": data["device_browser"],
                            }
                        ),
                    ),
                )
            self.event(batch_id, principal, "delivery_approved")
            return CommandResult(201, {"id": batch_id, "state": "APPROVED", "revision": 1})

        return self.service.command(
            principal,
            body.workspace_id,
            "execution.approve",
            body.command_id,
            "delivery_confirm",
            body.model_dump(mode="json"),
            apply,
        )

    def reapprove(self, workspace_id, batch_id, request, principal):
        body = DeliveryReapprove.model_validate(request)
        authorize(principal, workspace_id, "execution.approve")
        if principal.actor_type != "HUMAN" or body.workspace_id != workspace_id:
            raise WorkbenchError("HUMAN_REQUIRED", "Fresh approval requires the authenticated scoped human", 403)

        def apply():
            batch = self.store.db.execute(
                "SELECT * FROM wb_delivery_batches WHERE workspace_id=? AND id=?", (workspace_id, batch_id)
            ).fetchone()
            if batch is None:
                raise WorkbenchError("NOT_FOUND", "Delivery batch not found", 404)
            if batch["state"] != "NEEDS_REVIEW" or batch["revision"] != body.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Review the current delivery revision")
            signed = self.runtime.journal.decode(body.preview, max_length=500000)
            if (
                signed.get("kind") != "delivery-preview"
                or signed.get("version") != 1
                or signed.get("principal_id") != principal.id
                or signed.get("hash") != body.selection_hash
            ):
                raise WorkbenchError("INVALID_PREVIEW", "Fresh human review required", 422)
            reviewed = self.preview(signed["input"], principal)
            if reviewed["selection_hash"] != body.selection_hash or reviewed["blockers"]:
                raise WorkbenchError("STALE_PREVIEW", "Delivery selection changed", 422)
            data = reviewed["scope"]
            expiry = DeliveryPreview.model_validate(data["input"]).expires_at
            if data["input"]["project_id"] != batch["project_id"] or data["input"]["import_id"] != batch["import_id"]:
                raise WorkbenchError("INVALID_PREVIEW", "Review must retain the original feature scope", 422)
            for row in self.store.db.execute(
                "SELECT m.item_id,i.active_attempt_id,r.state FROM wb_delivery_members m JOIN wb_work_items i ON i.id=m.item_id LEFT JOIN wb_delivery_reservations r ON r.attempt_id=m.last_attempt_id WHERE m.batch_id=? AND m.active=1",
                (batch_id,),
            ):
                if row["active_attempt_id"] or row["state"] in {"reserved", "uncertain"}:
                    raise WorkbenchError(
                        "RECOVERY_REQUIRED", "Reconcile active or uncertain attempts before replacing approval", 422
                    )
            for member in data["members"]:
                prior = self.store.db.execute(
                    "SELECT batch_id FROM wb_delivery_members WHERE item_id=? AND active=1", (member["item_id"],)
                ).fetchone()
                if prior and prior[0] != batch_id:
                    raise WorkbenchError("CONSTRAINT_CONFLICT", "Ticket belongs to another active delivery")
            charged = self.store.db.execute(
                "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_microusd ELSE reserved_microusd END),0) FROM wb_delivery_reservations WHERE batch_id=?",
                (batch_id,),
            ).fetchone()[0]
            cap = budget_microusd(data["input"]["budget"])
            if cap < charged:
                raise WorkbenchError("BATCH_BUDGET_EXHAUSTED", "Reviewed cap is below historical delivery debit", 422)
            self.store.db.execute("UPDATE wb_delivery_members SET active=0 WHERE batch_id=?", (batch_id,))
            for member in data["members"]:
                self.store.db.execute(
                    "INSERT INTO wb_delivery_members(batch_id,item_id,approved_contract_revision,approved_contract_hash,allowed_agent_revisions_json,model_policy_hash,member_state) VALUES(?,?,?,?,?,?,'pending') ON CONFLICT(batch_id,item_id) DO UPDATE SET approved_contract_revision=excluded.approved_contract_revision,approved_contract_hash=excluded.approved_contract_hash,allowed_agent_revisions_json=excluded.allowed_agent_revisions_json,model_policy_hash=excluded.model_policy_hash,member_state='pending',last_attempt_id=NULL,active=1,blocked_reason=NULL",
                    (
                        batch_id,
                        member["item_id"],
                        member["contract_revision"],
                        member["contract_hash"],
                        json.dumps({member["agent_id"]: member["agent_revision"]}),
                        data["model_policy_hash"],
                    ),
                )
            self.store.db.execute(
                "UPDATE wb_delivery_grants SET revoked_at=? WHERE batch_id=? AND revoked_at IS NULL", (now(), batch_id)
            )
            grant_id = identity()
            self.store.db.execute(
                "INSERT INTO wb_delivery_grants(id,batch_id,principal_id,scope_hash,expires_at) VALUES(?,?,?,?,?)",
                (grant_id, batch_id, principal.id, body.selection_hash, expiry),
            )
            if data["device_id"]:
                self.store.db.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    (
                        "delivery-origin:" + grant_id,
                        json.dumps(
                            {
                                "device_id": data["device_id"],
                                "credential_id": data["device_credential_id"],
                                "browser": data["device_browser"],
                            }
                        ),
                    ),
                )
            self.store.db.execute(
                "UPDATE wb_delivery_batches SET state='APPROVED',revision=revision+1,approved_by=?,approved_at=?,expires_at=?,selection_hash=?,policy_hash=?,max_cost_microusd=?,updated_at=? WHERE id=?",
                (principal.id, now(), expiry, body.selection_hash, data["model_policy_hash"], cap, now(), batch_id),
            )
            self.event(batch_id, principal, "delivery_reapproved")
            return CommandResult(200, {"id": batch_id, "state": "APPROVED", "revision": batch["revision"] + 1})

        return self.service.command(
            principal,
            workspace_id,
            "execution.approve",
            body.command_id,
            "delivery_reapprove",
            {"id": batch_id, **body.model_dump(mode="json")},
            apply,
        )

    def authorization(self, batch_id, item_id, *, active_attempt=False):
        batch = self.store.db.execute("SELECT * FROM wb_delivery_batches WHERE id=?", (batch_id,)).fetchone()
        member = self.store.db.execute(
            "SELECT * FROM wb_delivery_members WHERE batch_id=? AND item_id=? AND active=1", (batch_id, item_id)
        ).fetchone()
        grant = self.store.db.execute(
            "SELECT * FROM wb_delivery_grants WHERE batch_id=? AND revoked_at IS NULL AND expires_at>?",
            (batch_id, now()),
        ).fetchone()
        allowed_states = {"APPROVED", "RUNNING", "PAUSED"} if active_attempt else {"APPROVED", "RUNNING"}
        if (
            batch is None
            or member is None
            or grant is None
            or batch["state"] not in allowed_states
            or batch["expires_at"] <= now()
            or grant["scope_hash"] != batch["selection_hash"]
        ):
            raise WorkbenchError("STALE_GRANT", "Delivery authorization expired or revoked", 422)
        if grant["principal_id"].startswith("device-"):
            origin = self.store.db.execute(
                "SELECT value FROM metadata WHERE key=?", ("delivery-origin:" + grant["id"],)
            ).fetchone()
            if origin is None:
                raise WorkbenchError("STALE_GRANT", "Delivery device session unavailable", 422)
            from .devices import DevicePrincipal

            data = json.loads(origin[0])
            DevicePrincipal(
                self.runtime.devices, data["device_id"], data["credential_id"], data["browser"], (), ()
            ).authorize(batch["workspace_id"], "execution.approve")
        if grant["device_credential_id"]:
            credential = self.store.db.execute(
                "SELECT device_id FROM wb_device_credentials WHERE id=?", (grant["device_credential_id"],)
            ).fetchone()
            if credential is None:
                raise WorkbenchError("STALE_GRANT", "Delivery device removed", 422)
            self.runtime.devices.current(credential[0], grant["device_credential_id"], False)
        item = self.store.get("work_item", batch["workspace_id"], item_id)
        agents = json.loads(member["allowed_agent_revisions_json"])
        if (
            item.contract_revision != member["approved_contract_revision"]
            or self.runtime.agents.policy_hash(item) != member["approved_contract_hash"]
        ):
            raise WorkbenchError("STALE_CONTRACT", "Delivery member changed", 422)
        if member["model_policy_hash"] != inventory_hash(self.runtime.engine.settings):
            raise WorkbenchError("POLICY_CHANGED", "Delivery model policy changed", 422)
        return batch, member, grant, agents

    def command(self, workspace_id, batch_id, request, principal):
        body = DeliveryCommand.model_validate(request)
        authorize(principal, workspace_id, "execution.approve")
        if principal.actor_type != "HUMAN":
            raise WorkbenchError("HUMAN_REQUIRED", "Delivery commands require an observed human", 403)

        def apply():
            batch = self.store.db.execute(
                "SELECT * FROM wb_delivery_batches WHERE workspace_id=? AND id=?", (workspace_id, batch_id)
            ).fetchone()
            if batch is None:
                raise WorkbenchError("NOT_FOUND", "Delivery batch not found", 404)
            if batch["revision"] != body.expected_revision:
                raise WorkbenchError("REVISION_CONFLICT", "Delivery batch changed")
            if batch["state"] in {"COMPLETED", "CANCELLED"}:
                raise WorkbenchError("INVALID_STATE", "Terminal batch requires a new preview")
            if body.action == "pause":
                if batch["state"] not in {"APPROVED", "RUNNING"}:
                    raise WorkbenchError("INVALID_STATE", "Only active delivery can pause")
                state = "PAUSED"
            elif body.action == "resume":
                if batch["state"] != "PAUSED":
                    raise WorkbenchError("INVALID_STATE", "Only paused delivery can resume")
                # Validate the original grant without enabling a concurrent start.
                self.store.db.execute("UPDATE wb_delivery_batches SET state='APPROVED' WHERE id=?", (batch_id,))
                for member in self.store.db.execute(
                    "SELECT item_id FROM wb_delivery_members WHERE batch_id=? AND active=1", (batch_id,)
                ):
                    self.authorization(batch_id, member[0])
                state = (
                    "RUNNING"
                    if self.store.db.execute(
                        "SELECT 1 FROM wb_delivery_members WHERE batch_id=? AND last_attempt_id IS NOT NULL LIMIT 1",
                        (batch_id,),
                    ).fetchone()
                    else "APPROVED"
                )
            else:
                state = "CANCELLED"
                self.store.db.execute(
                    "UPDATE wb_delivery_grants SET revoked_at=? WHERE batch_id=? AND revoked_at IS NULL",
                    (now(), batch_id),
                )
                for row in self.store.db.execute(
                    "SELECT i.id,i.active_attempt_id FROM wb_delivery_members m JOIN wb_work_items i ON i.id=m.item_id WHERE m.batch_id=? AND m.active=1",
                    (batch_id,),
                ):
                    if row["active_attempt_id"]:
                        self.store.db.execute(
                            "UPDATE wb_attempts SET cancel_requested_at=COALESCE(cancel_requested_at,?) WHERE id=?",
                            (now(), row["active_attempt_id"]),
                        )
                        self.service.changed_item(
                            self.store.get("work_item", workspace_id, row["id"]), {}, principal, "cancel_requested"
                        )
            self.store.db.execute(
                "UPDATE wb_delivery_batches SET state=?,revision=revision+1,updated_at=?,cancellation_reason=? WHERE id=?",
                (state, now(), body.reason if body.action == "cancel" else batch["cancellation_reason"], batch_id),
            )
            self.event(batch_id, principal, "delivery_" + body.action)
            return CommandResult(200, {"id": batch_id, "state": state, "revision": batch["revision"] + 1})

        return self.service.command(
            principal,
            workspace_id,
            "execution.approve",
            body.command_id,
            "delivery_command",
            {"id": batch_id, **body.model_dump(mode="json")},
            apply,
        )

    def tick(self):
        if not self.runtime.scheduler.leader():
            return
        from .scheduler import SYSTEM

        batches = self.store.db.execute(
            "SELECT id FROM wb_delivery_batches WHERE state<>'COMPLETED' AND EXISTS(SELECT 1 FROM wb_delivery_members m WHERE m.batch_id=wb_delivery_batches.id AND m.active=1) ORDER BY updated_at,id LIMIT 25"
        ).fetchall()
        remaining = 100
        for row in batches:
            progress_key = "delivery-progress:" + row[0]
            progress = self.store.db.execute("SELECT value FROM metadata WHERE key=?", (progress_key,)).fetchone()
            after = progress[0] if progress else ""
            members = self.store.db.execute(
                "SELECT item_id,last_attempt_id FROM wb_delivery_members WHERE batch_id=? AND active=1 AND item_id>? ORDER BY item_id LIMIT ?",
                (row[0], after, remaining),
            ).fetchall()
            if not members and after:
                members = self.store.db.execute(
                    "SELECT item_id,last_attempt_id FROM wb_delivery_members WHERE batch_id=? AND active=1 ORDER BY item_id LIMIT ?",
                    (row[0], remaining),
                ).fetchall()
            remaining -= len(members)
            for member in members:
                try:
                    with self.store.router.transaction():
                        if not self.runtime.scheduler.leader():
                            return
                        member = self.store.db.execute(
                            "SELECT item_id,last_attempt_id FROM wb_delivery_members WHERE batch_id=? AND item_id=? AND active=1",
                            (row[0], member["item_id"]),
                        ).fetchone()
                        if member is None:
                            continue
                        batch = self.store.db.execute(
                            "SELECT * FROM wb_delivery_batches WHERE id=?", (row[0],)
                        ).fetchone()
                        item = self.store.get("work_item", batch["workspace_id"], member["item_id"])
                        if member["last_attempt_id"]:
                            attempt = self.store.db.execute(
                                "SELECT state,request_id FROM wb_attempts WHERE id=?", (member["last_attempt_id"],)
                            ).fetchone()
                            if attempt["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
                                calls = self.runtime.engine.store.costs(attempt["request_id"])
                                charged = int(
                                    (Decimal(str(calls["estimated_usd"])) * 1000000).to_integral_value(
                                        rounding=ROUND_CEILING
                                    )
                                )
                                self.store.db.execute(
                                    "UPDATE wb_delivery_reservations SET state=?,settled_microusd=? WHERE attempt_id=?",
                                    (
                                        "uncertain" if calls["uncertain_calls"] else "settled",
                                        charged,
                                        member["last_attempt_id"],
                                    ),
                                )
                                if item.status == "DONE":
                                    self.store.db.execute(
                                        "UPDATE wb_delivery_members SET member_state='terminal',active=0 WHERE batch_id=? AND item_id=?",
                                        (batch["id"], item.id),
                                    )
                                elif (
                                    batch["state"] == "CANCELLED"
                                    and not item.active_attempt_id
                                    and not calls["uncertain_calls"]
                                ):
                                    self.store.db.execute(
                                        "UPDATE wb_delivery_members SET member_state='terminal',active=0 WHERE batch_id=? AND item_id=?",
                                        (batch["id"], item.id),
                                    )
                                elif attempt["state"] != "SUCCEEDED":
                                    raise WorkbenchError(
                                        "ATTEMPT_REVIEW_REQUIRED", "Delivery failed attempt needs new review", 422
                                    )
                            continue
                        if batch["state"] == "CANCELLED":
                            self.store.db.execute(
                                "UPDATE wb_delivery_members SET member_state='terminal',active=0 WHERE batch_id=? AND item_id=?",
                                (batch["id"], item.id),
                            )
                            continue
                        if batch["state"] not in {"APPROVED", "RUNNING"}:
                            continue
                        batch, _, _, agents = self.authorization(row[0], member["item_id"])
                        ready, reason = self.service.readiness(item)
                        if ready != "READY":
                            self.store.db.execute(
                                "UPDATE wb_delivery_members SET member_state='waiting',blocked_reason=? WHERE batch_id=? AND item_id=?",
                                (reason, batch["id"], item.id),
                            )
                            continue
                        if item.status == "BACKLOG":
                            item = self.service.changed_item(
                                item, {"status": "READY", "reason_code": None}, SYSTEM, "delivery_ready"
                            )
                        if item.status == "READY":
                            decision = self.runtime.agents.preview(batch["workspace_id"], item.id, SYSTEM)
                            agent_id = next(iter(agents))
                            rejected = next(
                                (entry for entry in decision["rejected"] if entry["agent_id"] == agent_id), None
                            )
                            if rejected and set(rejected["reasons"]) <= {"NO_CAPACITY", "AGENT_OFFLINE"}:
                                self.store.db.execute(
                                    "UPDATE wb_delivery_members SET member_state='waiting',blocked_reason=? WHERE batch_id=? AND item_id=?",
                                    (rejected["reasons"][0], batch["id"], item.id),
                                )
                                continue
                            self.runtime.agents.assign(
                                batch["workspace_id"],
                                item.id,
                                {
                                    "command_id": identity(),
                                    "expected_revision": item.revision,
                                    "agent_id": agent_id,
                                    "policy_revision_hash": decision["policy_revision_hash"],
                                },
                                SYSTEM,
                                _delivery_batch=batch["id"],
                            )
                            item = self.store.get("work_item", batch["workspace_id"], item.id)
                        self.runtime.execution.request(
                            batch["workspace_id"],
                            item.id,
                            {
                                "command_id": identity(),
                                "expected_revision": item.revision,
                                "command": "request_execution",
                            },
                            SYSTEM,
                            _delivery_batch=batch["id"],
                        )
                        if batch["state"] != "RUNNING":
                            self.store.db.execute(
                                "UPDATE wb_delivery_batches SET state='RUNNING',revision=revision+1,updated_at=? WHERE id=?",
                                (now(), batch["id"]),
                            )
                            self.event(batch["id"], SYSTEM, "delivery_running")
                except WorkbenchError as error:
                    with self.store.router.transaction():
                        self.store.db.execute(
                            "UPDATE wb_delivery_members SET member_state='needs_review',blocked_reason=? WHERE batch_id=? AND item_id=?",
                            (error.code, row[0], member["item_id"]),
                        )
                        self.store.db.execute(
                            "UPDATE wb_delivery_batches SET state='NEEDS_REVIEW',revision=revision+1,updated_at=? WHERE id=? AND state<>'CANCELLED'",
                            (now(), row[0]),
                        )
                        if (
                            self.store.db.execute(
                                "SELECT state FROM wb_delivery_batches WHERE id=?", (row[0],)
                            ).fetchone()[0]
                            != "CANCELLED"
                        ):
                            self.event(row[0], SYSTEM, "delivery_needs_review")
            with self.store.router.transaction():
                self.store.db.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                    (progress_key, members[-1]["item_id"] if members else ""),
                )
                self.store.db.execute("UPDATE wb_delivery_batches SET updated_at=? WHERE id=?", (now(), row[0]))
                if not self.store.db.execute(
                    "SELECT 1 FROM wb_delivery_members WHERE batch_id=? AND member_state<>'terminal' LIMIT 1", (row[0],)
                ).fetchone():
                    changed = self.store.db.execute(
                        "UPDATE wb_delivery_batches SET state='COMPLETED',revision=revision+1,updated_at=? WHERE id=? AND state IN ('APPROVED','RUNNING')",
                        (now(), row[0]),
                    )
                    if changed.rowcount:
                        self.event(row[0], SYSTEM, "delivery_completed")
            if remaining <= 0:
                break

    def get(self, workspace_id, batch_id, principal, *, after="", limit=50):
        authorize(principal, workspace_id, "workspace.read")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkbenchError("INVALID_PAGE", "Delivery page limit must be 1..100", 422)
        with self.store.router.lock:
            scope = {"workspace_id": workspace_id, "batch_id": batch_id, "principal_id": principal.id}
            if after:
                cursor = self.runtime.journal.decode(after)
                if (
                    not isinstance(cursor, dict)
                    or cursor.get("kind") != "delivery-page"
                    or cursor.get("scope") != scope
                    or type(cursor.get("expires")) is not int
                    or cursor["expires"] <= now()
                    or not isinstance(cursor.get("after"), str)
                ):
                    raise WorkbenchError("INVALID_CURSOR", "Delivery page scope changed or expired", 422)
                after = cursor["after"]
            batch = self.store.db.execute(
                "SELECT * FROM wb_delivery_batches WHERE workspace_id=? AND id=?", (workspace_id, batch_id)
            ).fetchone()
            if batch is None:
                raise WorkbenchError("NOT_FOUND", "Delivery batch not found", 404)
            members = self.store.db.execute(
                "SELECT m.item_id,m.member_state,m.blocked_reason,m.last_attempt_id,m.active,i.display_key,i.title,i.status,i.assigned_agent_id,f.agent_name,f.terminal_state AS attempt_state,f.priced_usage_cost_microusd,f.unknown_call_count,f.uncertain_call_count FROM wb_delivery_members m JOIN wb_work_items i ON i.id=m.item_id LEFT JOIN wb_attempt_facts f ON f.attempt_id=m.last_attempt_id WHERE m.batch_id=? AND m.item_id>? ORDER BY m.item_id LIMIT ?",
                (batch_id, after, limit + 1),
            ).fetchall()
            items = [dict(row) for row in members[:limit]]
            counts = dict(
                self.store.db.execute(
                    "SELECT member_state,COUNT(*) FROM wb_delivery_members WHERE batch_id=? GROUP BY member_state",
                    (batch_id,),
                )
            )
            budget = dict(
                self.store.db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_microusd ELSE 0 END),0) AS charged_microusd,COALESCE(SUM(CASE WHEN state<>'settled' THEN reserved_microusd ELSE 0 END),0) AS held_microusd,SUM(CASE WHEN state='uncertain' THEN 1 ELSE 0 END) AS uncertain_reservations FROM wb_delivery_reservations WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
            )
            next_cursor = (
                self.runtime.journal.token(
                    {"kind": "delivery-page", "scope": scope, "after": items[-1]["item_id"], "expires": now() + 900000}
                )
                if len(members) > limit
                else None
            )
            return {
                **dict(batch),
                "members": items,
                "counts": counts,
                "budget": budget,
                "total_members": sum(counts.values()),
                "has_more": len(members) > limit,
                "next_cursor": next_cursor,
            }
