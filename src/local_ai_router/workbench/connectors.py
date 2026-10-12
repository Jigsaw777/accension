"""Protocol-v1 proposals are untrusted data; the existing Engine owns all writes."""

import json
import time
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..contracts import ExecutionAborted
from ..errors import InvalidStructuredOutput
from ..schema import Strict, WorkerResult
from .schema import UUID, Hash, Identifier, Name, Timestamp, relative_path


class ConnectorUncertain(ExecutionAborted):
    code = "CONNECTOR_UNCERTAIN"


def build_request(store, model, task, capsule, plan, remaining):
    import hashlib
    import secrets

    from ..contracts import current_attempt_context
    from .service import payload_hash
    from .store import now

    context = current_attempt_context()
    if context is None:
        raise ExecutionAborted("Connector requires a host-owned Workbench attempt")
    files = {file["path"]: file for file in capsule["files"]}
    selected = [
        ConnectorFile(path=path, sha256=files[path]["original_sha256"], content=files[path]["content"])
        for path in task.expected_artifacts
    ]
    content_hash = payload_hash(
        {"task": task.model_dump(mode="json"), "files": [file.model_dump() for file in selected], "model": model.id}
    )
    dispatch_key = hashlib.sha256(
        (context["attempt_id"] + context["contract_hash"] + content_hash).encode()
    ).hexdigest()
    key = "connector-dispatch:" + dispatch_key
    with store.transaction():
        saved = store.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if saved:
            identity = ConnectorIdentity.model_validate_json(saved[0])
            if identity.expires_at <= now():
                raise ConnectorUncertain("Original connector dispatch expired; reconcile before retry")
        else:
            identity = ConnectorIdentity(
                protocol_version=1,
                request_id=dispatch_key,
                attempt_id=context["attempt_id"],
                dispatch_nonce=secrets.token_hex(32),
                expires_at=now() + store.settings.providers[model.provider].connector.timeout_seconds * 1000,
                contract_hash=context["contract_hash"],
            )
        request = ConnectorRequest(
            **identity.model_dump(),
            operation="propose_changes",
            task=ConnectorTask(id=task.id, objective=task.objective, done_when=task.acceptance_criteria),
            allowed_files=selected,
            privacy=plan.privacy_contract,
            constraints=plan.constraints,
            remaining_cap_microusd=int(remaining * 1000000),
        )
        # Persist only immutable dispatch metadata; context content remains host-local.
        store.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, identity.model_dump_json()))
        return request


class ConnectorTransport:
    async def generate(self, context, model, messages, maximum, schema=None, **kwargs):
        from ..contracts import current_proposal, execution_guard, normalized_role
        from ..schema import Generation, Usage

        request = current_proposal()
        if request is None or normalized_role(kwargs["role"]) not in {
            normalized_role(role) for role in context.provider.connector.roles
        }:
            raise ExecutionAborted("Connector cannot infer authority from user messages")
        execution_guard(model, kwargs["role"])
        observe_connector(context, request, "dispatch")
        try:
            response = await exchange(context, request)
            execution_guard(model, kwargs["role"])
        except BaseException as exc:
            observe_connector(
                context, request, "uncertain" if isinstance(exc, (ConnectorUncertain, ExecutionAborted)) else "rejected"
            )
            raise
        observe_connector(context, request, "reply")
        fields = response.usage.reported_fields
        usage = Usage.model_validate({name: getattr(response.usage, name) for name in fields})
        if usage.output_tokens > maximum:
            raise InvalidStructuredOutput("Connector exceeded approved output limit")
        return Generation(
            text=response.result.model_dump_json(),
            usage=usage,
            usage_source=response.usage.source,
            reported_usage_fields=fields,
            raw={
                "connector_cost": response.cost.model_dump(),
                "connector_id": context.name,
                "dispatch_nonce": request.dispatch_nonce,
            },
        )


def observe_connector(context, request, event):
    """Persist compact observations without remote claims, content or credentials."""
    from ..contracts import current_attempt_context
    from .activity import append_activity
    from .store import now

    store = context.manager.store
    stamp = now()
    with store.transaction():
        store.db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
            (
                "connector-health:" + context.name,
                json.dumps(
                    {"id": context.name, "status": "reachable" if event == "reply" else event, "observed_at": stamp}
                ),
            ),
        )
        attempt = store.db.execute(
            "SELECT workspace_id,item_id,contract_json,fence FROM wb_attempts WHERE id=?", (request.attempt_id,)
        ).fetchone()
        host = current_attempt_context()
        if attempt is None or host is None or attempt["fence"] != host["fence"]:
            return
        from .schema import WorkExecutionContract

        contract = WorkExecutionContract.model_validate_json(attempt["contract_json"])
        from .store import WorkbenchStore

        append_activity(
            WorkbenchStore(store),
            source_key="connector:" + request.dispatch_nonce + ":" + event,
            workspace_id=contract.workspace_id,
            project_id=contract.project_id,
            item_id=contract.item_id,
            agent_definition_id=contract.agent_definition_id,
            attempt_id=contract.attempt_id,
            occurred_at=stamp,
            actor_type="SYSTEM",
            actor_id="engine",
            event_type="connector_" + event,
            metadata={
                "stage": "connector_" + event,
                "model_id": context.name,
                "summary": "Connector " + context.name + " · " + event + "; host validation and review remain separate",
            },
        )


def connector_profiles(runtime, definition):
    bindings = definition.model_bindings
    model_ids = {
        model
        for _, binding in bindings
        if binding.mode == "pinned"
        for model in [binding.primary_model_id, *binding.fallback_model_ids]
    }
    providers = {
        model.provider
        for model in runtime.engine.settings.models
        if model.id in model_ids and runtime.engine.settings.providers[model.provider].kind == "connector"
    }
    result = []
    for name in sorted(providers):
        provider = runtime.engine.settings.providers[name]
        saved = runtime.engine.store.db.execute(
            "SELECT value FROM metadata WHERE key=?", ("connector-health:" + name,)
        ).fetchone()
        observation = json.loads(saved[0]) if saved else {"id": name, "status": "unknown", "observed_at": None}
        result.append(
            {
                **observation,
                "name": provider.connector.name,
                "roles": provider.connector.roles,
                "local": provider.local,
                "host_capacity_is_remote_health": False,
            }
        )
    return result


async def exchange(context, request):
    """Only the registered origin is contacted; polling never accepts a callback URL."""
    import asyncio
    from urllib.parse import quote

    from ..config import validate_endpoint
    from ..privacy import guard_provider
    from .store import now

    provider = context.provider
    validate_endpoint(provider, context.settings.port)
    guard_provider(context.settings, provider, "executor")
    registration = provider.connector
    raw = request_bytes(request, registration)
    base = provider.endpoint.rstrip("/")
    headers = await context.headers()
    headers = {
        **headers,
        "Content-Type": "application/json",
        "Idempotency-Key": request.request_id + ":" + request.dispatch_nonce,
    }

    async def read(method, url, content=None):
        async with context.client.stream(
            method, url, headers=headers, content=content, timeout=registration.timeout_seconds, follow_redirects=False
        ) as response:
            if response.is_redirect:
                raise InvalidStructuredOutput("Connector redirects are prohibited")
            response.raise_for_status()
            if int(response.headers.get("content-length", "0")) > registration.max_response_bytes:
                raise InvalidStructuredOutput("Connector response exceeds registered limit")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > registration.max_response_bytes:
                    raise InvalidStructuredOutput("Connector response exceeds registered limit")
            try:
                return validate_response(bytes(chunks), request, registration, now())
            except ValueError as exc:
                raise InvalidStructuredOutput("Connector response failed identity, scope or schema validation") from exc

    started = time.monotonic()
    try:
        result = await read("POST", base, raw)
        job_id = result.job_id
        for _ in range(registration.max_polls):
            if result.status == "completed":
                return result
            if time.monotonic() - started >= registration.timeout_seconds or now() >= request.expires_at:
                raise ConnectorUncertain("Connector accepted job needs reconciliation")
            await asyncio.sleep(min(0.25, max(0, registration.timeout_seconds - (time.monotonic() - started))))
            result = await read("GET", base + "/jobs/" + quote(job_id, safe=""))
            if result.job_id and result.job_id != job_id:
                raise InvalidStructuredOutput("Connector changed accepted job identity")
        raise ConnectorUncertain("Connector polling limit reached; reconcile original job")
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        raise ConnectorUncertain("Connector outcome unknown; reconcile original dispatch") from exc
    except Exception as exc:
        import httpx

        if isinstance(exc, httpx.TransportError):
            raise ConnectorUncertain("Connector outcome unknown; reconcile original dispatch") from exc
        raise


class ConnectorTask(Strict):
    id: Identifier
    objective: Annotated[str, Field(min_length=1, max_length=20000)]
    done_when: Annotated[list[str], Field(max_length=100)] = []


class ConnectorFile(Strict):
    path: Annotated[str, Field(min_length=1, max_length=512)]
    sha256: Hash | None
    content: Annotated[str, Field(max_length=500000)] | None

    @model_validator(mode="after")
    def relative_path(self):
        relative_path(self.path)
        return self


class ConnectorIdentity(Strict):
    protocol_version: Literal[1]
    request_id: Name
    attempt_id: UUID
    dispatch_nonce: Hash
    expires_at: Timestamp
    contract_hash: Hash


class ConnectorRequest(ConnectorIdentity):
    operation: Literal["propose_changes"]
    task: ConnectorTask
    allowed_files: Annotated[list[ConnectorFile], Field(max_length=24)]
    privacy: Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"]
    constraints: Annotated[list[str], Field(max_length=100)] = []
    remaining_cap_microusd: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def unique_files(self):
        if len({file.path for file in self.allowed_files}) != len(self.allowed_files):
            raise ValueError("Connector allowed files must be unique")
        return self


class ConnectorUsage(Strict):
    source: Literal["connector_reported", "unknown"] = "unknown"
    reported_fields: Annotated[
        list[Literal["input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens"]], Field(max_length=4)
    ] = []
    input_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    output_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    cached_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    cache_write_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def known_fields(self):
        if (
            len(set(self.reported_fields)) != len(self.reported_fields)
            or any(getattr(self, name) is None for name in self.reported_fields)
            or (self.source == "unknown" and self.reported_fields)
        ):
            raise ValueError("Connector usage provenance must match reported fields")
        return self


class ConnectorCost(Strict):
    source: Literal["connector_reported", "unknown"] = "unknown"
    microusd: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def known_cost(self):
        if (self.source == "unknown") != (self.microusd is None):
            raise ValueError("Connector unknown cost must remain null")
        return self


class ConnectorResponse(ConnectorIdentity):
    status: Literal["accepted", "completed"]
    job_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")] | None = None
    result: WorkerResult | None = None
    usage: ConnectorUsage = Field(default_factory=ConnectorUsage)
    cost: ConnectorCost = Field(default_factory=ConnectorCost)

    @model_validator(mode="after")
    def status_result(self):
        if (self.status == "accepted" and (not self.job_id or self.result is not None)) or (
            self.status == "completed" and self.result is None
        ):
            raise ValueError("Connector status requires its matching job or result")
        return self


def validate_response(raw, request, registration, stamp):
    if len(raw) > registration.max_response_bytes:
        raise ValueError("Connector response exceeds registered size limit")
    response = ConnectorResponse.model_validate_json(raw)
    if stamp >= request.expires_at:
        raise ValueError("Connector dispatch expired")
    if response.model_dump(include=set(ConnectorIdentity.model_fields)) != request.model_dump(
        include=set(ConnectorIdentity.model_fields)
    ):
        raise ValueError("Connector reply identity or contract changed")
    if response.result:
        allowed = {file.path: file.sha256 for file in request.allowed_files}
        seen = set()
        for change in response.result.files_changed:
            if change.path not in allowed or change.path in seen or change.original_sha256 != allowed[change.path]:
                raise ValueError("Connector proposal path or base hash denied")
            seen.add(change.path)
        if response.cost.microusd is not None and response.cost.microusd > request.remaining_cap_microusd:
            raise ValueError("Connector claimed cost exceeds approved cap")
    return response


def request_bytes(request, registration):
    raw = json.dumps(request.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False).encode()
    if len(raw) > registration.max_request_bytes:
        raise ValueError("Connector request exceeds registered size limit")
    return raw
