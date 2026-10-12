"""Thin local HTTP adapters over the same scoped Workbench command service."""

from __future__ import annotations

import base64
import hashlib
import json
from functools import wraps
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import TypeAdapter, ValidationError

from .policy import CAPABILITIES, Principal, authorize
from .schema import UUID, ActorType, Identifier
from .service import CommandResult
from .store import WorkbenchError

RESOURCES = {
    "workspaces": "workspace",
    "projects": "project",
    "prds": "prd",
    "work-items": "work_item",
    "comments": "comment",
}
PREFIXES = ("/accs/v1/workbench", "/ui/workbench/api")
uuid_adapter = TypeAdapter(UUID)


def local_principal(request):
    if getattr(request.state, "service_authenticated", False):
        if (
            request.url.path.endswith(("/prds/operator-approval", "/work-items/operator-command"))
            and request.headers.get("x-workbench-operator-action") == "human"
        ):
            return Principal("local-cli", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)
        return Principal(
            "service", ActorType.MACHINE, CAPABILITIES - {"execution.approve", "review.approve"}, frozenset({"*"}), True
        )
    if request.url.path.startswith("/ui/") and getattr(request.state, "ui_session", None):
        session_id = hashlib.sha256(request.cookies.get("accension_session", "").encode()).hexdigest()
        return Principal("browser-" + session_id, ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)
    raise WorkbenchError("UNAUTHENTICATED", "Authenticated local session or service token required", 401)


def transport(handler):
    @wraps(handler)
    async def bounded(request: Request, **kwargs):
        try:
            result = await handler(request, **kwargs)
            if isinstance(result, CommandResult):
                return JSONResponse(result.body, result.status)
            return result
        except WorkbenchError as exc:
            code, message, status = (
                exc.code,
                {
                    "REVISION_CONFLICT": "Entity changed; refresh before retrying.",
                    "FORBIDDEN": "Action is not granted to this principal.",
                    "HUMAN_REQUIRED": "An observed human action is required.",
                }.get(exc.code, "Workbench request rejected; check the error code and refresh."),
                exc.status,
            )
        except ValidationError as exc:
            code, message, status = (
                "INVALID_SCHEMA",
                "; ".join(".".join(map(str, e["loc"])) + ": " + e["type"] for e in exc.errors()),
                400,
            )
        except (ValueError, TypeError):
            code, message, status = "INVALID_INPUT", "Invalid request fields or contract", 400
        except HTTPException as exc:
            code, message, status = "INVALID_REQUEST", "Invalid request.", exc.status_code
        return JSONResponse(
            {"error": {"code": code, "message": message}, "request_id": request.headers.get("x-request-id", "")[:128]},
            status,
        )

    return bounded


def cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_cursor(value, kind):
    if value is None:
        return None
    if len(value) > 512:
        raise WorkbenchError("INVALID_PAGE", "Cursor exceeds limit", 422)
    try:
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if kind == "work_item":
            if not isinstance(data, list) or len(data) != 2 or type(data[0]) is not int or not 0 <= data[0] <= 4:
                raise ValueError()
            return data[0], uuid_adapter.validate_python(data[1])
        return uuid_adapter.validate_python(data)
    except (ValueError, TypeError) as exc:
        raise WorkbenchError("INVALID_PAGE", "Invalid cursor", 422) from exc


def install(app, runtime, *, prefixes=PREFIXES, principal_resolver=local_principal, route_filter=None):
    from ..app import bounded_json
    from .templates import get as get_template
    from .templates import load_template, register

    service = runtime.service
    from .board import BoardQueries
    from .schema import BoardQuery

    boards = BoardQueries(runtime)

    @transport
    async def models_read(request: Request):
        from .model_options import model_options

        fields = dict(request.query_params)
        cursor = fields.pop("cursor", None)
        if "limit" in fields:
            fields["limit"] = int(fields["limit"])
        return model_options(runtime, fields, principal_resolver(request), cursor=cursor)

    @transport
    async def activity_read(request: Request, entity_id: str):
        from .activity import timeline

        workspace = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        entity = uuid_adapter.validate_python(entity_id)
        fields = {"item_id" if "/work-items/" in request.url.path else "agent_id": entity}
        return timeline(
            runtime,
            workspace,
            principal_resolver(request),
            **fields,
            cursor=request.query_params.get("cursor"),
            limit=int(request.query_params.get("limit", 50)),
            project_id=request.query_params.get("project_id"),
            start=int(request.query_params["from"]) if "from" in request.query_params else None,
            end=int(request.query_params["to"]) if "to" in request.query_params else None,
        )

    @transport
    async def board_read(request: Request, state: str | None = None):
        fields = dict(request.query_params)
        token = fields.pop("cursor", None)
        for field in ("limit", "priority"):
            if field in fields:
                fields[field] = int(fields[field])
        if "states" in fields:
            fields["states"] = fields["states"].split(",")
        query = BoardQuery.model_validate(fields)
        return JSONResponse(boards.page(query, principal_resolver(request), state=state, cursor=token))

    def add_route(path, handler, *, methods):
        if route_filter is None or route_filter(path, methods[0]):
            app.add_api_route(path, handler, methods=methods)

    @transport
    async def devices_read(request: Request):
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        return runtime.devices.listing(
            workspace_id, principal_resolver(request), request.url.path.endswith("/pairings")
        )

    @transport
    async def lan_setup(request: Request):
        actor = principal_resolver(request)
        authorize(actor, "local", "device.admin", local_only=True)
        config = runtime.engine.settings.workbench_lan
        return {
            "enabled": config.enabled,
            "origins": config.origins,
            "workspaces": config.workspaces,
            "error": runtime.lan_error,
            "restart_required": True,
            "instructions": "Configure workbench_lan in local.yaml with a trusted certificate/key, exact HTTPS origins, bind IP, separate port and permitted workspace IDs. Restart the daemon. Trust the issuing CA on each client; then enter the HTTPS URL and one-time code. Never bypass certificate validation.",
        }

    @transport
    async def retention_preview(request: Request):
        return runtime.retention.preview(
            principal_resolver(request), int(request.query_params.get("before_sequence", "0"))
        )

    @transport
    async def retention_apply(request: Request):
        return runtime.retention.apply(await bounded_json(request), principal_resolver(request))

    @transport
    async def devices_command(request: Request):
        from typing import Literal

        from pydantic import Field

        from ..schema import Strict
        from .schema import Revision

        class DeviceCommand(Strict):
            workspace_id: UUID
            command_id: UUID
            action: Literal["invite", "approve", "deny", "revoke"]
            id: UUID | None = None
            expected_revision: Revision | None = None
            phrase: str = Field(default="", max_length=80)
            capabilities: list[Identifier] = Field(default=["workspace.read"], max_length=20)

        body = DeviceCommand.model_validate(await bounded_json(request))
        if body.action != "invite" and (body.id is None or body.expected_revision is None):
            raise WorkbenchError("INVALID_SCHEMA", "Identity and expected revision required", 422)
        return runtime.devices.operator(body.workspace_id, body, principal_resolver(request))

    @transport
    async def attempts_read(request: Request, entity_id: str):
        return runtime.execution.attempts(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
            int(request.query_params.get("limit", "50")),
            int(request.query_params["cursor"]) if request.query_params.get("cursor") else None,
        )

    @transport
    async def attempt_read(request: Request, entity_id: str):
        section = request.url.path.rsplit("/", 1)[-1]
        return runtime.execution.read_attempt(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
            section if section in {"receipt", "verification", "artifacts"} else None,
        )

    @transport
    async def artifact_read(request: Request, entity_id: str):
        return runtime.execution.artifact_content(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
        )

    @transport
    async def privacy_read(request: Request):
        principal = principal_resolver(request)
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        authorize(principal, workspace_id, "workspace.read")
        workspace = service.store.get("workspace", workspace_id, workspace_id)
        return {
            "workspace_id": workspace_id,
            "privacy_mode": workspace.privacy_mode,
            "inference_policy": workspace.inference_policy,
            "review_policy": workspace.review_policy,
            "execution_policy": workspace.execution_policy.model_dump(mode="json"),
            "inference": False,
        }

    @transport
    async def operator_work_command(request: Request):
        from typing import Union

        from ..schema import Strict
        from .schema import CompletionDecision, ExecutionApproval

        class Operator(Strict):
            workspace_id: UUID
            item_id: UUID
            command: Union[ExecutionApproval, CompletionDecision]

        body = Operator.model_validate(await bounded_json(request))
        return service.item_command(body.workspace_id, body.item_id, body.command, principal_resolver(request))

    @transport
    async def item_evidence(request: Request, entity_id: str):
        principal = principal_resolver(request)
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        authorize(principal, workspace_id, "artifact.read")
        item = service.get("work_item", workspace_id, uuid_adapter.validate_python(entity_id), principal)
        row, data = service.evidence(item)
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "evidence_hash": row["evidence_hash"],
            "contract_revision": item.contract_revision,
            "evidence": data,
        }

    @transport
    async def attempt_recovery(request: Request, entity_id: str):
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        return runtime.execution.recovery_request(
            workspace_id,
            uuid_adapter.validate_python(entity_id),
            await bounded_json(request),
            principal_resolver(request),
        )

    @transport
    async def dispatch_command(request: Request, resource: str, entity_id: str):
        if resource not in {"workspaces", "projects"}:
            raise WorkbenchError("NOT_FOUND", "Dispatch resource not found", 404)
        workspace_id = uuid_adapter.validate_python(
            entity_id if resource == "workspaces" else request.query_params.get("workspace_id")
        )
        return runtime.scheduler.dispatch(
            RESOURCES[resource],
            workspace_id,
            uuid_adapter.validate_python(entity_id),
            await bounded_json(request),
            principal_resolver(request),
        )

    @transport
    async def execution_preflight(request: Request, entity_id: str):
        return runtime.agents.execution_preflight(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
        )

    @transport
    async def team_lead_proposal(request: Request):
        return runtime.planning.team_lead(await bounded_json(request), principal_resolver(request))

    @transport
    async def assignment_preview(request: Request, entity_id: str):
        return runtime.agents.preview(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
        )

    @transport
    async def assignment_commit(request: Request, entity_id: str):
        return runtime.agents.assign(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            await bounded_json(request),
            principal_resolver(request),
        )

    @transport
    async def agents_list(request: Request):
        fields = request.query_params
        enabled = fields.get("enabled")
        if enabled not in {None, "true", "false"}:
            raise WorkbenchError("INVALID_PAGE", "Enabled filter must be true or false", 422)
        return runtime.agents.list_page(
            uuid_adapter.validate_python(fields.get("workspace_id")),
            principal_resolver(request),
            limit=int(fields.get("limit", 50)),
            cursor=fields.get("cursor"),
            query=fields.get("query", ""),
            enabled=None if enabled is None else enabled == "true",
        )

    @transport
    async def agent_configure(request: Request, entity_id: str | None = None):
        return runtime.agents.configure(
            await bounded_json(request),
            principal_resolver(request),
            uuid_adapter.validate_python(entity_id) if entity_id else None,
        )

    @transport
    async def agent_read(request: Request, entity_id: str):
        if entity_id == "configure":
            raise WorkbenchError("NOT_FOUND", "Agent not found", 404)
        return runtime.agents.profile(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
        )

    @transport
    async def metrics_read(request: Request, entity_id: str):
        from .metrics import agent_metrics
        from .schema import parse_timestamp

        fields = request.query_params
        return agent_metrics(
            runtime,
            uuid_adapter.validate_python(fields.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
            start=parse_timestamp(fields["from"]) if not fields["from"].isdigit() else int(fields["from"]),
            end=parse_timestamp(fields["to"]) if not fields["to"].isdigit() else int(fields["to"]),
            bucket=fields.get("bucket", "day"),
            project_id=uuid_adapter.validate_python(fields["project_id"]) if fields.get("project_id") else None,
            execution_kind=fields.get("execution_kind"),
        )

    @transport
    async def agent_options(request: Request):
        actor = principal_resolver(request)
        authorize(actor, "local", "agent.admin", local_only=True)
        return {
            "tools": sorted(set(service.settings.tool_registry) | {"propose_file_changes"}),
            "skills": [
                {"id": skill["id"], "name": skill["name"]}
                for skill in service.skills.list(limit=100)["items"]
                if skill["enabled"] and skill["trusted"]
            ],
            "capabilities": ["code", "text", "structured_output", "tools", "vision", "embeddings"],
        }

    @transport
    async def import_preview(request: Request):
        return runtime.planning.preview(await bounded_json(request), principal_resolver(request))

    @transport
    async def reconcile_preview(request: Request, entity_id: str):
        body = await bounded_json(request)
        if body.get("prd_id") != entity_id:
            raise WorkbenchError("INVALID_INPUT", "Reconciliation PRD identity mismatch", 400)
        return runtime.planning.preview(body, principal_resolver(request), reconcile=True)

    @transport
    async def import_read(request: Request, entity_id: str):
        return service.get(
            "plan_import",
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
        ).model_dump(mode="json")

    @transport
    async def import_commit(request: Request, entity_id: str):
        return runtime.planning.commit(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            await bounded_json(request),
            principal_resolver(request),
            reconcile=request.url.path.endswith("/reconcile-commit"),
        )

    @transport
    async def delivery_preview(request: Request):
        return runtime.delivery.preview(await bounded_json(request), principal_resolver(request))

    @transport
    async def deliveries_read(request: Request):
        fields = request.query_params
        return runtime.delivery.list(
            uuid_adapter.validate_python(fields.get("workspace_id")),
            principal_resolver(request),
            project_id=uuid_adapter.validate_python(fields["project_id"]) if fields.get("project_id") else None,
            cursor=fields.get("cursor"),
            limit=int(fields.get("limit", 50)),
        )

    @transport
    async def delivery_confirm(request: Request):
        return runtime.delivery.confirm(await bounded_json(request), principal_resolver(request))

    @transport
    async def delivery_read(request: Request, entity_id: str):
        return runtime.delivery.get(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
            after=request.query_params.get("cursor", ""),
            limit=int(request.query_params.get("limit", "50")),
        )

    @transport
    async def delivery_command(request: Request, entity_id: str):
        method = runtime.delivery.reapprove if request.url.path.endswith("/reapprove") else runtime.delivery.command
        return method(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            await bounded_json(request),
            principal_resolver(request),
        )

    @transport
    async def prd_imports(request: Request, entity_id: str):
        workspace = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        principal = principal_resolver(request)
        authorize(principal, workspace, "workspace.read")
        with service.store.router.lock:
            service.store.get("prd", workspace, uuid_adapter.validate_python(entity_id))
            rows = service.store.db.execute(
                "SELECT id,project_id FROM wb_plan_imports WHERE workspace_id=? AND prd_id=? AND status='COMMITTED' ORDER BY created_at DESC,id DESC LIMIT 50",
                (workspace, entity_id),
            ).fetchall()
            return {
                "items": [
                    {
                        **dict(row),
                        "item_ids": [
                            item[0]
                            for item in service.store.db.execute(
                                "SELECT work_item_id FROM wb_plan_bindings WHERE import_id=? ORDER BY work_item_id LIMIT 1000",
                                (row["id"],),
                            )
                        ],
                    }
                    for row in rows
                ]
            }

    @transport
    async def planning_quote(request: Request):
        return runtime.planning.quote(await bounded_json(request), principal_resolver(request))

    @transport
    async def planning_generate(request: Request):
        body = await bounded_json(request)
        if body.get("kind", "generate") != "generate":
            raise WorkbenchError("INVALID_INPUT", "Generation kind mismatch", 400)
        return runtime.planning.enqueue(body, principal_resolver(request))

    @transport
    async def planning_decompose(request: Request, entity_id: str):
        body = await bounded_json(request)
        if body.get("prd_id") != entity_id or body.get("kind") != "decompose":
            raise WorkbenchError("INVALID_INPUT", "Decomposition identity/kind mismatch", 400)
        return runtime.planning.enqueue(body, principal_resolver(request))

    @transport
    async def operation_read(request: Request, entity_id: str):
        return runtime.planning.get(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            principal_resolver(request),
        )

    @transport
    async def operations_read(request: Request):
        fields = request.query_params
        return runtime.planning.list(
            uuid_adapter.validate_python(fields.get("workspace_id")),
            principal_resolver(request),
            project_id=uuid_adapter.validate_python(fields["project_id"]) if fields.get("project_id") else None,
            limit=int(fields.get("limit", 50)),
            cursor=fields.get("cursor"),
        )

    @transport
    async def snapshot_start(request: Request):
        from ..schema import Strict
        from .events import SnapshotFilter

        class Snapshot(Strict):
            workspace_id: UUID
            filters: SnapshotFilter = SnapshotFilter()
            limit: int = 50

        body = Snapshot.model_validate(await bounded_json(request))
        return runtime.journal.start_snapshot(
            body.workspace_id, principal_resolver(request), body.filters.model_dump(), limit=body.limit
        )

    @transport
    async def snapshot_page(request: Request, snapshot_id: str):
        token = request.query_params.get("cursor", "")
        if runtime.journal.decode(token)["lease"] != snapshot_id:
            raise WorkbenchError("INVALID_SNAPSHOT", "Snapshot cursor identity mismatch", 422)
        return runtime.journal.snapshot_page(
            token, principal_resolver(request), limit=int(request.query_params.get("limit", "50"))
        )

    @transport
    async def activity(request: Request):
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        return {
            "items": runtime.journal.events(
                workspace_id,
                principal_resolver(request),
                after=int(request.query_params.get("after", "0")),
                limit=int(request.query_params.get("limit", "200")),
            )
        }

    @transport
    async def event_stream(request: Request):
        principal = principal_resolver(request)
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        supplied, header = request.query_params.get("after"), request.headers.get("last-event-id")
        if supplied is not None and header is not None and supplied != header:
            raise WorkbenchError("INVALID_CURSOR", "Conflicting event cursors", 422)
        after = int(supplied or header or "0")
        runtime.journal.events(workspace_id, principal, after=after, limit=1)
        return StreamingResponse(
            runtime.journal.stream(workspace_id, principal, after, disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @transport
    async def status(request: Request):
        principal = principal_resolver(request)
        workspace_id = uuid_adapter.validate_python(
            request.query_params.get("workspace_id", "00000000-0000-4000-8000-000000000001")
        )
        authorize(principal, workspace_id, "workspace.read")
        with service.store.router.lock:
            counts = dict(
                service.store.db.execute(
                    "SELECT status,COUNT(*) FROM wb_work_items WHERE workspace_id=? AND archived_at IS NULL GROUP BY status",
                    (workspace_id,),
                )
            )
        return {
            "workspace_id": workspace_id,
            "counts": counts,
            "runtime": "started" if runtime.started else "stopped",
            "dispatcher": "running" if runtime.scheduler.leader() else "stopped",
            "dispatcher_error": runtime.scheduler.error,
        }

    @transport
    async def repositories(request: Request):
        principal = principal_resolver(request)
        authorize(principal, "local", "repository.admin", local_only=True)
        return {
            "items": [
                {
                    "id": key,
                    "name": Path(repo.path).name,
                    "validation_checks": list(repo.validation),
                }
                for key, repo in service.repositories().items()
            ]
        }

    def handlers(kind):
        @transport
        async def listing(request: Request):
            principal = principal_resolver(request)
            if kind == "workspace":
                limit = int(request.query_params.get("limit", "50"))
                if not 1 <= limit <= 200:
                    raise WorkbenchError("INVALID_PAGE", "Page size must be between 1 and 200", 422)
                after = decode_cursor(request.query_params.get("cursor"), kind)
                search = request.query_params.get("query", "")
                if len(search) > 200:
                    raise WorkbenchError("INVALID_SEARCH", "Search limited to 200 characters", 422)
                literal = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                with service.store.router.lock:
                    if principal.local and "*" in principal.workspaces:
                        rows = service.store.db.execute(
                            "SELECT id FROM wb_workspaces WHERE archived_at IS NULL AND id>? AND name LIKE ? ESCAPE '\\' ORDER BY id LIMIT ?",
                            (after or "", literal, limit + 1),
                        ).fetchall()
                    else:
                        rows = [(wid,) for wid in sorted(principal.workspaces) if wid > (after or "")][: limit + 1]
                    items = [service.get(kind, row[0], row[0], principal) for row in rows[:limit]]
                return {
                    "items": [item.model_dump(mode="json") for item in items],
                    "has_more": len(rows) > limit,
                    "next_cursor": cursor(items[-1].id) if len(rows) > limit and items else None,
                }
            workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
            limit = int(request.query_params.get("limit", "50"))
            if not 1 <= limit <= 200:
                raise WorkbenchError("INVALID_PAGE", "Page size must be between 1 and 200", 422)
            filters = {
                "project_id": request.query_params.get("project_id"),
                "status": request.query_params.get("status"),
                "search": request.query_params.get("query", ""),
                "after": decode_cursor(request.query_params.get("cursor"), kind),
                "limit": limit,
            }
            if request.query_params.get("priority") is not None:
                filters["priority"] = int(request.query_params["priority"])
            if request.query_params.get("agent_id"):
                filters["agent_id"] = uuid_adapter.validate_python(request.query_params["agent_id"])
            if request.query_params.get("type"):
                filters["item_type"] = request.query_params["type"]
            if request.query_params.get("item_id"):
                filters["item_id"] = uuid_adapter.validate_python(request.query_params["item_id"])
            items = service.list(kind, workspace_id, principal, **filters)
            # Peek one bounded row after the page; no page ever materializes history.
            next_key = (
                (items[-1].priority, items[-1].id) if kind == "work_item" and items else items[-1].id if items else None
            )
            more = bool(
                items and service.list(kind, workspace_id, principal, **{**filters, "after": next_key, "limit": 1})
            )
            from .events import projection

            return {
                "items": [
                    projection(kind, item) if kind == "work_item" else item.model_dump(mode="json") for item in items
                ],
                "has_more": more,
                "next_cursor": cursor(next_key) if more else None,
            }

        @transport
        async def create(request: Request):
            return service.create(kind, await bounded_json(request), principal_resolver(request))

        @transport
        async def read(request: Request, entity_id: str):
            entity_id = uuid_adapter.validate_python(entity_id)
            workspace_id = (
                entity_id
                if kind == "workspace"
                else uuid_adapter.validate_python(request.query_params.get("workspace_id"))
            )
            principal = principal_resolver(request)
            entity = service.get(kind, workspace_id, entity_id, principal)
            body = entity.model_dump(mode="json")
            if kind == "work_item":
                body["allowed_actions"] = service.allowed_actions(entity, principal)
            return body

        @transport
        async def update(request: Request, entity_id: str):
            entity_id = uuid_adapter.validate_python(entity_id)
            workspace_id = (
                entity_id
                if kind == "workspace"
                else uuid_adapter.validate_python(request.query_params.get("workspace_id"))
            )
            return service.update(
                kind, workspace_id, entity_id, await bounded_json(request), principal_resolver(request)
            )

        @transport
        async def archive(request: Request, entity_id: str):
            entity_id = uuid_adapter.validate_python(entity_id)
            workspace_id = (
                entity_id
                if kind == "workspace"
                else uuid_adapter.validate_python(request.query_params.get("workspace_id"))
            )
            return service.archive(
                kind, workspace_id, entity_id, await bounded_json(request), principal_resolver(request)
            )

        @transport
        async def revisions(request: Request, entity_id: str):
            entity_id = uuid_adapter.validate_python(entity_id)
            workspace_id = (
                entity_id
                if kind == "workspace"
                else uuid_adapter.validate_python(request.query_params.get("workspace_id"))
            )
            items = service.revisions(
                kind,
                workspace_id,
                entity_id,
                principal_resolver(request),
                limit=int(request.query_params.get("limit", "50")),
                before=int(request.query_params["before"]) if "before" in request.query_params else None,
            )
            return {
                "items": [item.model_dump(mode="json") for item in items],
                "next_cursor": items[-1].revision if items else None,
            }

        return listing, create, read, update, archive, revisions

    @transport
    async def commands(request: Request, entity_id: str, resource: str):
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        entity_id = uuid_adapter.validate_python(entity_id)
        if resource == "prds":
            return service.prd_command(
                workspace_id, entity_id, await bounded_json(request), principal_resolver(request)
            )
        if resource == "work-items":
            return service.item_command(
                workspace_id, entity_id, await bounded_json(request), principal_resolver(request)
            )
        raise WorkbenchError("NOT_FOUND", "Command resource not found", 404)

    @transport
    async def dependencies(request: Request, entity_id: str):
        return service.dependencies(
            uuid_adapter.validate_python(request.query_params.get("workspace_id")),
            uuid_adapter.validate_python(entity_id),
            await bounded_json(request),
            principal_resolver(request),
        )

    @transport
    async def by_key(request: Request, display_key: str):
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        principal = principal_resolver(request)
        authorize(principal, workspace_id, "workspace.read")
        with service.store.router.lock:
            row = service.store.db.execute(
                "SELECT id FROM wb_work_items WHERE workspace_id=? AND display_key=?", (workspace_id, display_key)
            ).fetchone()
            if row is None:
                raise WorkbenchError("NOT_FOUND", "Work item not found", 404)
            return service.get("work_item", workspace_id, row[0], principal).model_dump(mode="json")

    @transport
    async def dependency_list(request: Request, entity_id: str):
        workspace_id = uuid_adapter.validate_python(request.query_params.get("workspace_id"))
        service.get("work_item", workspace_id, uuid_adapter.validate_python(entity_id), principal_resolver(request))
        with service.store.router.lock:
            rows = service.store.db.execute(
                "SELECT blocker_id,dependent_id FROM wb_dependencies WHERE workspace_id=? AND (dependent_id=? OR blocker_id=?) ORDER BY blocker_id,dependent_id LIMIT 200",
                (workspace_id, entity_id, entity_id),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    @transport
    async def operator_prd_approval(request: Request):
        from ..schema import Strict
        from .schema import PRDCommand

        class Approval(Strict):
            workspace_id: UUID
            prd_id: UUID
            command: PRDCommand

        body = Approval.model_validate(await bounded_json(request))
        return service.prd_command(body.workspace_id, body.prd_id, body.command, principal_resolver(request))

    @transport
    async def templates(request: Request):
        principal = principal_resolver(request)
        workspace_id = request.query_params.get("workspace_id", "local")
        authorize(principal, workspace_id, "workspace.read")
        with service.store.router.lock:
            rows = service.store.db.execute(
                "SELECT id,kind,version,content_hash FROM wb_templates WHERE enabled=1 ORDER BY id,version LIMIT 200"
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    @transport
    async def template_preview(request: Request):
        from ..schema import Strict

        class Preview(Strict):
            definition: str
            spec: dict = {}

        principal = principal_resolver(request)
        authorize(principal, "local", "workspace.read", local_only=True)
        body = Preview.model_validate(await bounded_json(request))
        return {"sections": load_template(body.definition).preview(body.spec)}

    @transport
    async def template_import(request: Request):
        from ..schema import Strict

        class Import(Strict):
            command_id: UUID
            definition: str

        principal = principal_resolver(request)
        body = Import.model_validate(await bounded_json(request))
        template = load_template(body.definition)

        def apply():
            register(service.store.db, template, "local-import")
            return CommandResult(
                201, {"id": template.id, "version": template.version, "content_hash": template.content_hash}
            )

        return service.command(
            principal,
            "local",
            "policy.admin",
            body.command_id,
            "template_import",
            body.model_dump(mode="json"),
            apply,
            local_only=True,
        )

    @transport
    async def template_export(request: Request, template_id: str):
        principal = principal_resolver(request)
        authorize(principal, "local", "workspace.read", local_only=True)
        template = get_template(
            service.store.db,
            TypeAdapter(Identifier).validate_python(template_id),
            int(request.query_params.get("version", "1")),
        )
        import yaml

        return PlainTextResponse(
            yaml.safe_dump(template.model_dump(mode="json"), sort_keys=False), media_type="application/yaml"
        )

    for prefix in prefixes:
        add_route(prefix + "/work-items/{entity_id}/activity", activity_read, methods=["GET"])
        add_route(prefix + "/agents/{entity_id}/activity", activity_read, methods=["GET"])
        add_route(prefix + "/board", board_read, methods=["GET"])
        add_route(prefix + "/board/columns/{state}", board_read, methods=["GET"])
        add_route(prefix + "/retention/preview", retention_preview, methods=["GET"])
        add_route(prefix + "/retention/apply", retention_apply, methods=["POST"])
        add_route(prefix + "/lan/setup", lan_setup, methods=["GET"])
        add_route(prefix + "/devices", devices_read, methods=["GET"])
        add_route(prefix + "/pairings", devices_read, methods=["GET"])
        add_route(prefix + "/devices/commands", devices_command, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/attempts", attempts_read, methods=["GET"])
        for suffix in ("", "/receipt", "/verification", "/artifacts"):
            add_route(prefix + "/attempts/{entity_id}" + suffix, attempt_read, methods=["GET"])
        add_route(prefix + "/artifacts/{entity_id}/content", artifact_read, methods=["GET"])
        add_route(prefix + "/privacy/diagnostics", privacy_read, methods=["GET"])
        if prefix == "/accs/v1/workbench":
            add_route(prefix + "/work-items/operator-command", operator_work_command, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/evidence", item_evidence, methods=["GET"])
        add_route(prefix + "/attempts/{entity_id}/recovery", attempt_recovery, methods=["POST"])
        add_route(prefix + "/{resource}/{entity_id}/dispatch", dispatch_command, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/execution-preflight", execution_preflight, methods=["POST"])
        add_route(prefix + "/team-lead/proposals", team_lead_proposal, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/assignment-preview", assignment_preview, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/assign", assignment_commit, methods=["POST"])
        add_route(prefix + "/agents", agents_list, methods=["GET"])
        add_route(prefix + "/operations", operations_read, methods=["GET"])
        add_route(prefix + "/agents/{entity_id}", agent_read, methods=["GET"])
        add_route(prefix + "/agents/{entity_id}/metrics", metrics_read, methods=["GET"])
        add_route(prefix + "/agent-options", agent_options, methods=["GET"])
        add_route(prefix + "/model-options", models_read, methods=["GET"])
        add_route(prefix + "/agents", agent_configure, methods=["POST"])
        add_route(prefix + "/agents/{entity_id}", agent_configure, methods=["PATCH"])
        add_route(prefix + "/plan-imports", import_preview, methods=["POST"])
        add_route(prefix + "/plan-imports/{entity_id}", import_read, methods=["GET"])
        add_route(prefix + "/plan-imports/{entity_id}/commit", import_commit, methods=["POST"])
        add_route(prefix + "/plan-imports/{entity_id}/reconcile-commit", import_commit, methods=["POST"])
        add_route(prefix + "/delivery-batches/preview", delivery_preview, methods=["POST"])
        add_route(prefix + "/delivery-batches", delivery_confirm, methods=["POST"])
        add_route(prefix + "/delivery-batches", deliveries_read, methods=["GET"])
        add_route(prefix + "/delivery-batches/{entity_id}", delivery_read, methods=["GET"])
        add_route(prefix + "/delivery-batches/{entity_id}/commands", delivery_command, methods=["POST"])
        add_route(prefix + "/delivery-batches/{entity_id}/reapprove", delivery_command, methods=["POST"])
        add_route(prefix + "/prds/{entity_id}/imports", prd_imports, methods=["GET"])
        add_route(prefix + "/prds/{entity_id}/reconcile", reconcile_preview, methods=["POST"])
        add_route(prefix + "/planning/quote", planning_quote, methods=["POST"])
        add_route(prefix + "/prds/generate", planning_generate, methods=["POST"])
        add_route(prefix + "/prds/{entity_id}/decompose", planning_decompose, methods=["POST"])
        add_route(prefix + "/operations/{entity_id}", operation_read, methods=["GET"])
        add_route(prefix + "/snapshots", snapshot_start, methods=["POST"])
        add_route(prefix + "/snapshots/{snapshot_id}", snapshot_page, methods=["GET"])
        add_route(prefix + "/activity", activity, methods=["GET"])
        add_route(prefix + "/events", event_stream, methods=["GET"])
        add_route(prefix + "/work-items/by-key/{display_key}", by_key, methods=["GET"])
        if prefix == "/accs/v1/workbench":
            add_route(prefix + "/prds/operator-approval", operator_prd_approval, methods=["POST"])
        add_route(prefix + "/status", status, methods=["GET"])
        add_route(prefix + "/repositories", repositories, methods=["GET"])
        add_route(prefix + "/templates", templates, methods=["GET"])
        add_route(prefix + "/templates/preview", template_preview, methods=["POST"])
        add_route(prefix + "/templates/import", template_import, methods=["POST"])
        add_route(prefix + "/templates/{template_id}/export", template_export, methods=["GET"])
        for resource, kind in RESOURCES.items():
            listing, create, read, update, archive, revisions = handlers(kind)
            path = prefix + "/" + resource
            for suffix, method, handler in (
                ("", "GET", listing),
                ("", "POST", create),
                ("/{entity_id}", "GET", read),
                ("/{entity_id}", "PATCH", update),
                ("/{entity_id}/archive", "POST", archive),
                ("/{entity_id}/revisions", "GET", revisions),
            ):
                if kind == "prd" and suffix.endswith("archive"):
                    continue
                add_route(path + suffix, handler, methods=[method])
        add_route(prefix + "/{resource}/{entity_id}/commands", commands, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/dependencies", dependencies, methods=["POST"])
        add_route(prefix + "/work-items/{entity_id}/dependencies", dependency_list, methods=["GET"])
