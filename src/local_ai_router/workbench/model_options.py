"""Configured model choices reuse Engine eligibility without inference or discovery."""

from ..contracts import contract_scope, strictest
from ..privacy import is_local
from ..schema import Classification
from .policy import authorize
from .schema import ModelOption, ModelOptionsQuery, budget_microusd
from .service import payload_hash
from .store import WorkbenchError, now


def inventory_hash(settings):
    return payload_hash(
        {
            "models": [model.model_dump(mode="json") for model in settings.models],
            "providers": {name: provider.model_dump(mode="json") for name, provider in settings.providers.items()},
            "roles": {name: policy.model_dump(mode="json") for name, policy in settings.roles.items()},
            "routing": settings.routing.model_dump(mode="json"),
            "privacy": settings.privacy.model_dump(mode="json"),
        }
    )


def validate_bindings(runtime, definition, principal):
    for role, selection in definition.model_bindings:
        if selection.mode == "auto":
            continue
        ids = [selection.primary_model_id, *selection.fallback_model_ids]
        for workspace_id in definition.allowed_workspaces:
            projects = [None]
            if definition.allowed_projects:
                projects.extend(
                    project_id
                    for project_id in definition.allowed_projects
                    if runtime.service.store.db.execute(
                        "SELECT 1 FROM wb_projects WHERE workspace_id=? AND id=?", (workspace_id, project_id)
                    ).fetchone()
                )
            for model_id in ids:
                for project_id in projects:
                    options = model_options(
                        runtime,
                        {
                            "workspace_id": workspace_id,
                            "purpose": role,
                            **({"project_id": project_id} if project_id else {}),
                            "query": model_id,
                            "limit": 100,
                        },
                        principal,
                        privacy=definition.privacy_policy,
                    )
                    row = next((row for row in options["items"] if row["id"] == model_id), None)
                    if row is None or not row["eligible"]:
                        raise WorkbenchError(
                            "MODEL_INELIGIBLE", "Pinned model is unknown or ineligible for " + role, 422
                        )


def model_options(runtime, request, principal, *, cursor=None, privacy=None):
    query = ModelOptionsQuery.model_validate(request)
    authorize(principal, query.workspace_id, "workspace.read")
    store, settings = runtime.service.store, runtime.engine.settings
    workspace = store.get("workspace", query.workspace_id, query.workspace_id)
    project = store.get("project", query.workspace_id, query.project_id) if query.project_id else None
    repository = runtime.service.repository(project.repository_ref) if project and project.repository_ref else None
    mode = strictest(
        workspace.inference_policy,
        project.default_privacy if project else None,
        repository.privacy_mode if repository else None,
        privacy,
    )
    if workspace.privacy_mode in {"DEVICE_ONLY", "PRIVATE_LAN"}:
        mode = "LOCAL_ONLY"
    cap = int(settings.budgets.default_request_budget * 1_000_000)
    for value in (query.budget, workspace.execution_policy.budget_ceiling):
        if value is not None:
            cap = min(cap, budget_microusd(value))
    purpose = "repairer" if query.purpose == "repair" else query.purpose
    scope = {
        "kind": "models",
        "version": 1,
        "principal": principal.id,
        "query": payload_hash({**query.model_dump(exclude={"limit"}), "purpose": purpose, "privacy": mode, "cap": cap}),
        "inventory": inventory_hash(settings),
    }
    after = ""
    if cursor:
        position = runtime.journal.decode(cursor)
        if (
            not isinstance(position, dict)
            or any(position.get(key) != value for key, value in scope.items())
            or not isinstance(position.get("after"), str)
            or type(position.get("expires")) is not int
            or position["expires"] < now()
        ):
            raise WorkbenchError("CURSOR_QUERY_MISMATCH", "Model scope or inventory changed", 422)
        after = position["after"]
    with store.router.lock, contract_scope(mode=mode):
        _, report = runtime.engine.router.roles.select(
            Classification(task_family="coding"), purpose, allow_cloud=mode != "LOCAL_ONLY", budget=cap / 1_000_000
        )
    reasons = {row["model"]: row for row in report["candidates"]}
    selected = sorted(
        (
            model
            for model in settings.models
            if model.id > after
            and query.query.casefold() in (model.id + " " + model.provider + " " + model.deployment_name).casefold()
        ),
        key=lambda model: model.id,
    )
    items = []
    for model in selected[: query.limit]:
        provider = settings.providers.get(model.provider)
        row = reasons[model.id]
        codes = list(row["reason_codes"])
        if query.adapter != "engine":
            codes.append("ADAPTER_UNAVAILABLE")
        priced = runtime.engine.router.roles.priced(model) if provider else model
        items.append(
            ModelOption(
                id=model.id,
                provider=model.provider,
                deployment=model.deployment_name,
                locality="unknown" if provider is None else "local" if is_local(provider) else "cloud",
                enabled=model.enabled and bool(provider and provider.enabled),
                eligible=not codes,
                reason_codes=codes,
                pricing_status=priced.pricing_status,
                estimated_max_cost=row["estimated_max_cost"],
            ).model_dump(mode="json")
        )
    more = len(selected) > query.limit
    return {
        "items": items,
        "has_more": more,
        "next_cursor": runtime.journal.token({**scope, "after": items[-1]["id"], "expires": now() + 900_000})
        if more
        else None,
        "purpose": purpose,
        "privacy": mode,
        "model_policy_hash": scope["inventory"],
        "inference": False,
    }
