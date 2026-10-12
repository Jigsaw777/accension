"""Machine Workbench tools; stdio forwards instead of starting an execution owner."""

from .client import WorkbenchClient
from .policy import CAPABILITIES, Principal
from .schema import UUID, ActorType


def install(mcp, settings, shared_engine=None):
    principal = Principal(
        "mcp", ActorType.MACHINE, CAPABILITIES - {"execution.approve", "review.approve"}, frozenset({"*"}), True
    )

    def service():
        from .runtime import WorkbenchRuntime

        runtime = getattr(shared_engine, "workbench_runtime", None) or WorkbenchRuntime(shared_engine)
        return runtime.service

    async def forward(method, path, *, params=None, body=None):
        client = WorkbenchClient(settings)
        try:
            return await client.request(method, path, params=params, body=body)
        finally:
            await client.close()

    @mcp.tool()
    async def list_projects(workspace_id: UUID, limit: int = 50, cursor: str | None = None) -> dict:
        """List scoped projects without inference or repository edits."""
        if shared_engine is None:
            return await forward(
                "GET",
                "/projects",
                params={"workspace_id": workspace_id, "limit": limit, **({"cursor": cursor} if cursor else {})},
            )
        from .api import decode_cursor

        items = service().list("project", workspace_id, principal, limit=limit, after=decode_cursor(cursor, "project"))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @mcp.tool()
    async def get_project(workspace_id: UUID, project_id: UUID) -> dict:
        """Read a project; no inference or repository edits."""
        if shared_engine is None:
            return await forward("GET", "/projects/" + project_id, params={"workspace_id": workspace_id})
        return service().get("project", workspace_id, project_id, principal).model_dump(mode="json")

    @mcp.tool()
    async def create_prd(workspace_id: UUID, project_id: UUID, command_id: UUID, title: str, spec: dict) -> dict:
        """Create an unapproved draft PRD; writes Workbench state, no inference."""
        body = {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "command_id": command_id,
            "title": title,
            "spec": spec,
        }
        if shared_engine is None:
            return await forward("POST", "/prds", body=body)
        return service().create("prd", body, principal).body

    @mcp.tool()
    async def get_prd(workspace_id: UUID, prd_id: UUID) -> dict:
        """Read current PRD revision; no inference."""
        if shared_engine is None:
            return await forward("GET", "/prds/" + prd_id, params={"workspace_id": workspace_id})
        return service().get("prd", workspace_id, prd_id, principal).model_dump(mode="json")

    @mcp.tool()
    async def list_work_items(
        workspace_id: UUID,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List bounded scoped work items; no inference."""
        params = {"workspace_id": workspace_id, "limit": limit}
        params.update(
            {
                key: value
                for key, value in {"project_id": project_id, "status": status, "cursor": cursor}.items()
                if value is not None
            }
        )
        if shared_engine is None:
            return await forward("GET", "/work-items", params=params)
        from .api import decode_cursor

        items = service().list(
            "work_item",
            workspace_id,
            principal,
            project_id=project_id,
            status=status,
            limit=limit,
            after=decode_cursor(cursor, "work_item"),
        )
        return {"items": [item.model_dump(mode="json") for item in items]}

    @mcp.tool()
    async def create_work_item(workspace_id: UUID, project_id: UUID, command_id: UUID, fields: dict) -> dict:
        """Create a BACKLOG item from strict fields; no inference or execution."""
        body = {**fields, "workspace_id": workspace_id, "project_id": project_id, "command_id": command_id}
        if shared_engine is None:
            return await forward("POST", "/work-items", body=body)
        return service().create("work_item", body, principal).body

    @mcp.tool()
    async def get_work_item(workspace_id: UUID, item_id: UUID) -> dict:
        """Read a work item and its server-computed allowed actions; no inference."""
        if shared_engine is None:
            return await forward("GET", "/work-items/" + item_id, params={"workspace_id": workspace_id})
        item = service().get("work_item", workspace_id, item_id, principal)
        return {**item.model_dump(mode="json"), "allowed_actions": service().allowed_actions(item, principal)}

    @mcp.tool()
    async def update_work_item(
        workspace_id: UUID, item_id: UUID, command_id: UUID, expected_revision: int, fields: dict
    ) -> dict:
        """Edit strict work item fields with revision and retry protection; no inference."""
        body = {**fields, "command_id": command_id, "expected_revision": expected_revision}
        if shared_engine is None:
            return await forward("PATCH", "/work-items/" + item_id, params={"workspace_id": workspace_id}, body=body)
        return service().update("work_item", workspace_id, item_id, body, principal).body

    @mcp.tool()
    async def cancel_work_item(workspace_id: UUID, item_id: UUID, command_id: UUID, expected_revision: int) -> dict:
        """Request work cancellation; writes state, preserving evidence and existing files."""
        body = {"command_id": command_id, "expected_revision": expected_revision, "command": "cancel"}
        if shared_engine is None:
            return await forward(
                "POST", "/work-items/" + item_id + "/commands", params={"workspace_id": workspace_id}, body=body
            )
        return service().item_command(workspace_id, item_id, body, principal).body

    @mcp.tool()
    async def get_work_item_status(workspace_id: UUID, item_id: UUID) -> dict:
        """Read current Workbench status without executing a task or inference."""
        item = await get_work_item(workspace_id, item_id)
        return {
            key: item[key] for key in ("id", "display_key", "revision", "status", "active_attempt_id", "reason_code")
        }

    @mcp.tool()
    async def propose_assignment(workspace_id: UUID, item_id: UUID) -> dict:
        """Preview eligible agents and policy hash; no inference or reservation."""
        if shared_engine is None:
            return await forward(
                "POST", "/work-items/" + item_id + "/assignment-preview", params={"workspace_id": workspace_id}
            )
        return service().agents.preview(workspace_id, item_id, principal)

    @mcp.tool()
    async def assign_work_item(
        workspace_id: UUID,
        item_id: UUID,
        command_id: UUID,
        expected_revision: int,
        agent_id: UUID,
        policy_revision_hash: str,
    ) -> dict:
        """Reserve an eligible agent using current policy and revision; no inference."""
        body = {
            "command_id": command_id,
            "expected_revision": expected_revision,
            "agent_id": agent_id,
            "policy_revision_hash": policy_revision_hash,
        }
        if shared_engine is None:
            return await forward(
                "POST", "/work-items/" + item_id + "/assign", params={"workspace_id": workspace_id}, body=body
            )
        return service().agents.assign(workspace_id, item_id, body, principal).body

    @mcp.tool()
    async def execute_work_item(workspace_id: UUID, item_id: UUID, command_id: UUID, expected_revision: int) -> dict:
        """Queue a granted current attempt; Engine may edit approved files and incur provider costs. Human approval cannot come from this tool."""
        body = {"command_id": command_id, "expected_revision": expected_revision, "command": "request_execution"}
        if shared_engine is None:
            return await forward(
                "POST", "/work-items/" + item_id + "/commands", params={"workspace_id": workspace_id}, body=body
            )
        return service().item_command(workspace_id, item_id, body, principal).body

    @mcp.tool()
    async def work_item_command(
        workspace_id: UUID,
        item_id: UUID,
        command_id: UUID,
        expected_revision: int,
        command: str,
        fields: dict | None = None,
    ) -> dict:
        """Apply a strict lifecycle command. Machine actors cannot approve execution, attest manual work, or approve completion."""
        body = {**(fields or {}), "command_id": command_id, "expected_revision": expected_revision, "command": command}
        if shared_engine is None:
            return await forward(
                "POST", "/work-items/" + item_id + "/commands", params={"workspace_id": workspace_id}, body=body
            )
        return service().item_command(workspace_id, item_id, body, principal).body

    @mcp.tool()
    async def list_agents(workspace_id: UUID, limit: int = 50, cursor: str | None = None, query: str = "") -> dict:
        """Read configured agents and observed worker availability; no provider calls."""
        if shared_engine is None:
            return await forward(
                "GET",
                "/agents",
                params={
                    "workspace_id": workspace_id,
                    "limit": limit,
                    "query": query,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
        return service().agents.list_page(workspace_id, principal, limit=limit, cursor=cursor, query=query)

    @mcp.tool()
    async def get_agent_profile(workspace_id: UUID, agent_id: UUID) -> dict:
        """Read scoped configuration, current work and connector observations; no inference."""
        if shared_engine is None:
            return await forward("GET", "/agents/" + agent_id, params={"workspace_id": workspace_id})
        return service().agents.profile(workspace_id, agent_id, principal)

    @mcp.tool()
    async def get_agent_status(workspace_id: UUID, agent_id: UUID) -> dict:
        """Read one scoped agent's real heartbeat state; no inference."""
        result = await get_agent_profile(workspace_id, agent_id)
        return {**result["definition"], "load": result["load"], "capacity": result["capacity"]}

    @mcp.tool()
    async def get_workbench_board(
        workspace_id: UUID,
        project_id: UUID | None = None,
        query: str = "",
        limit: int = 20,
        state: str | None = None,
        cursor: str | None = None,
    ) -> dict:
        """Read compact scoped columns with bounded signed pagination; no execution."""
        from .board import BoardQueries
        from .schema import BoardQuery

        fields = {
            "workspace_id": workspace_id,
            "query": query,
            "limit": limit,
            **({"project_id": project_id} if project_id else {}),
        }
        if shared_engine is None:
            return await forward(
                "GET",
                "/board" + ("/columns/" + state if state else ""),
                params={**fields, **({"cursor": cursor} if cursor else {})},
            )
        service()
        return BoardQueries(shared_engine.workbench_runtime).page(
            BoardQuery.model_validate(fields), principal, state=state, cursor=cursor
        )

    @mcp.tool()
    async def get_workbench_timeline(
        workspace_id: UUID,
        entity_id: UUID,
        entity_type: str = "work_item",
        project_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """Read bounded redacted ticket or agent history; no provider calls."""
        from .activity import timeline
        from .store import WorkbenchError

        if entity_type not in {"work_item", "agent"}:
            raise WorkbenchError("INVALID_INPUT", "Expected work_item or agent", 422)
        fields = {
            "workspace_id": workspace_id,
            "limit": limit,
            **({"project_id": project_id} if project_id else {}),
            **({"cursor": cursor} if cursor else {}),
        }
        if shared_engine is None:
            return await forward(
                "GET",
                ("/agents/" if entity_type == "agent" else "/work-items/") + entity_id + "/activity",
                params=fields,
            )
        service()
        return timeline(
            shared_engine.workbench_runtime,
            workspace_id,
            principal,
            limit=limit,
            cursor=cursor,
            project_id=project_id,
            **{"agent_id" if entity_type == "agent" else "item_id": entity_id},
        )

    @mcp.tool()
    async def get_agent_metrics(
        workspace_id: UUID,
        agent_id: UUID,
        start: int,
        end: int,
        bucket: str = "day",
        project_id: UUID | None = None,
        execution_kind: str | None = None,
    ) -> dict:
        """Read measured denominators and known or unknown costs in an explicit time window."""
        from .metrics import agent_metrics

        if shared_engine is None:
            return await forward(
                "GET",
                "/agents/" + agent_id + "/metrics",
                params={
                    "workspace_id": workspace_id,
                    "from": start,
                    "to": end,
                    "bucket": bucket,
                    **({"project_id": project_id} if project_id else {}),
                    **({"execution_kind": execution_kind} if execution_kind else {}),
                },
            )
        service()
        return agent_metrics(
            shared_engine.workbench_runtime,
            workspace_id,
            agent_id,
            principal,
            start=start,
            end=end,
            bucket=bucket,
            project_id=project_id,
            execution_kind=execution_kind,
        )

    @mcp.tool()
    async def get_workbench_model_options(input: dict, cursor: str | None = None) -> dict:
        """List eligible registered models; never discovers providers or invokes inference."""
        from .model_options import model_options

        if shared_engine is None:
            return await forward("GET", "/model-options", params={**input, **({"cursor": cursor} if cursor else {})})
        service()
        return model_options(shared_engine.workbench_runtime, input, principal, cursor=cursor)

    @mcp.tool()
    async def preview_feature_delivery(input: dict) -> dict:
        """Preview current imported feature contracts without granting approval or execution."""
        if shared_engine is None:
            return await forward("POST", "/delivery-batches/preview", body=input)
        service()
        return shared_engine.workbench_runtime.delivery.preview(input, principal)

    @mcp.tool()
    async def get_feature_delivery(workspace_id: UUID, batch_id: UUID, limit: int = 50, cursor: str = "") -> dict:
        """Read approved batch progress; completion remains subject to host review."""
        if shared_engine is None:
            return await forward(
                "GET",
                "/delivery-batches/" + batch_id,
                params={"workspace_id": workspace_id, "limit": limit, "cursor": cursor},
            )
        service()
        return shared_engine.workbench_runtime.delivery.get(
            workspace_id, batch_id, principal, limit=limit, after=cursor
        )

    @mcp.tool()
    async def quote_workbench_planning(input: dict) -> dict:
        """Quote schema-bound PRD generation or decomposition; no paid inference."""
        if shared_engine is None:
            return await forward("POST", "/planning/quote", body=input)
        service()
        return shared_engine.workbench_runtime.planning.quote(input, principal)

    @mcp.tool()
    async def generate_work_items(input: dict, command_id: UUID, quote: str) -> dict:
        """Request PRD generation or approved-PRD decomposition, with explicit quote/budget. May incur inference cost; returns an operation, never executes edits."""
        body = {**input, "command_id": command_id, "quote": quote}
        if shared_engine is None:
            return await forward(
                "POST",
                "/prds/generate"
                if body.get("kind", "generate") == "generate"
                else "/prds/" + body["prd_id"] + "/decompose",
                body=body,
            )
        service()
        return shared_engine.workbench_runtime.planning.enqueue(body, principal).body

    @mcp.tool()
    async def get_workbench_operation(workspace_id: UUID, operation_id: UUID) -> dict:
        """Read a durable operation's result and actual cost without triggering inference."""
        if shared_engine is None:
            return await forward("GET", "/operations/" + operation_id, params={"workspace_id": workspace_id})
        service()
        return shared_engine.workbench_runtime.planning.get(workspace_id, operation_id, principal)

    @mcp.tool()
    async def preview_work_import(input: dict, command_id: UUID, reconcile: bool = False) -> dict:
        """Stage strict plan import/reconciliation without changing items or executing files."""
        body = {**input, "command_id": command_id}
        if shared_engine is None:
            return await forward(
                "POST", "/prds/" + body["prd_id"] + "/reconcile" if reconcile else "/plan-imports", body=body
            )
        service()
        return shared_engine.workbench_runtime.planning.preview(body, principal, reconcile=reconcile).body

    @mcp.tool()
    async def commit_work_import(
        workspace_id: UUID,
        import_id: UUID,
        command_id: UUID,
        expected_revision: int,
        source_hash: str,
        actions: dict,
        item_revisions: dict | None = None,
        reconcile: bool = False,
    ) -> dict:
        """Commit explicitly selected create/update/keep/archive/successor actions atomically; no inference or repository edits."""
        body = {
            "command_id": command_id,
            "expected_revision": expected_revision,
            "source_hash": source_hash,
            "actions": actions,
            "item_revisions": item_revisions or {},
        }
        if shared_engine is None:
            return await forward(
                "POST",
                "/plan-imports/" + import_id + ("/reconcile-commit" if reconcile else "/commit"),
                params={"workspace_id": workspace_id},
                body=body,
            )
        service()
        return shared_engine.workbench_runtime.planning.commit(
            workspace_id, import_id, body, principal, reconcile=reconcile
        ).body

    @mcp.tool()
    async def list_work_attempts(workspace_id: UUID, item_id: UUID, limit: int = 50, before: int | None = None) -> dict:
        """Read bounded attempt history; no execution or inference."""
        if shared_engine is None:
            return await forward(
                "GET",
                "/work-items/" + item_id + "/attempts",
                params={"workspace_id": workspace_id, "limit": limit, **({"cursor": before} if before else {})},
            )
        return service().execution.attempts(workspace_id, item_id, principal, limit, before)

    @mcp.tool()
    async def get_work_attempt(workspace_id: UUID, attempt_id: UUID, section: str | None = None) -> dict:
        """Read observed attempt, immutable receipt, verification or artifacts, with scope checks; no inference."""
        from .store import WorkbenchError

        if section not in {None, "receipt", "verification", "artifacts"}:
            raise WorkbenchError("INVALID_INPUT", "Unknown attempt section", 422)
        if shared_engine is None:
            return await forward(
                "GET",
                "/attempts/" + attempt_id + ("/" + section if section else ""),
                params={"workspace_id": workspace_id},
            )
        return service().execution.read_attempt(workspace_id, attempt_id, principal, section)

    @mcp.tool()
    async def get_work_artifact(workspace_id: UUID, artifact_id: UUID) -> dict:
        """Read bounded redacted UTF-8 artifact by authorized ID, verifying containment and hash."""
        if shared_engine is None:
            return await forward("GET", "/artifacts/" + artifact_id + "/content", params={"workspace_id": workspace_id})
        return service().execution.artifact_content(workspace_id, artifact_id, principal)

    @mcp.tool()
    async def recover_work_attempt(
        workspace_id: UUID, attempt_id: UUID, command_id: UUID, expected_revision: int, action: str
    ) -> dict:
        """Request privileged inspect/resume/rollback/discard through owning daemon. Resume can incur inference; rollback/discard are explicit file recovery actions."""
        body = {"command_id": command_id, "expected_revision": expected_revision, "action": action}
        if shared_engine is None:
            return await forward(
                "POST", "/attempts/" + attempt_id + "/recovery", params={"workspace_id": workspace_id}, body=body
            )
        return service().execution.recovery_request(workspace_id, attempt_id, body, principal).body
