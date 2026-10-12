"""Execution-local restrictions and transactional, metadata-only egress accounting."""

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from .errors import PrivacyViolation
from .schema import EgressBudget, uid

ORDER = {"LOCAL_ONLY": 0, "CLOUD_REDACTED": 1, "CLOUD_ALLOWED": 2}
_contract = ContextVar("accension_execution_contract", default=None)
_execution = ContextVar("accension_workbench_execution", default=None)
_observer = ContextVar("accension_execution_observer", default=None)
_model_selections = ContextVar("accension_model_selections", default=None)
_proposal = ContextVar("accension_host_proposal", default=None)
_attempt_context = ContextVar("accension_attempt_context", default=None)


@dataclass(frozen=True)
class ExecutionSignal:
    kind: Literal[
        "execution_start",
        "worker_start",
        "validation_start",
        "validation_end",
        "review_start",
        "review_end",
        "execution_end",
        "receipt_finalized",
    ]
    task_id: str | None = None
    model_id: str | None = None
    checks: tuple = ()
    status: str | None = None
    event_id: str = field(default_factory=uid)


@contextmanager
def observer_scope(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def execution_signal(kind, **fields):
    observer = _observer.get()
    if observer is not None:
        if kind != "receipt_finalized":
            execution_guard()
        observer(ExecutionSignal(kind, **fields))


class ExecutionAborted(RuntimeError):
    """Lost authorization must abort, never enter an Engine repair loop."""


@contextmanager
def execution_scope(
    guard, roles, models=None, tools=None, required_review=False, *, model_bindings=None, attempt_context=None
):
    token = _execution.set(
        {
            "guard": guard,
            "roles": set(roles),
            "models": set(models) if models is not None else None,
            "tools": set(tools or []),
            "required_review": required_review,
        }
    )
    attempt_token = _attempt_context.set(attempt_context)
    try:
        with model_selection_scope(model_bindings or {}):
            yield
    finally:
        _execution.reset(token)
        _attempt_context.reset(attempt_token)


def current_attempt_context():
    return _attempt_context.get()


@contextmanager
def proposal_scope(request):
    token = _proposal.set(request)
    try:
        yield
    finally:
        _proposal.reset(token)


def current_proposal():
    return _proposal.get()


def review_required():
    return bool((_execution.get() or {}).get("required_review"))


def normalized_role(role):
    return {"repair": "repairer", "fast_executor": "executor", "complex_executor": "executor"}.get(role, role)


@contextmanager
def model_selection_scope(bindings):
    selected = dict(_model_selections.get() or {})
    for role, binding in bindings.items():
        if binding["mode"] == "auto":
            continue
        role = normalized_role(role)
        order = [binding["primary_model_id"], *binding.get("fallback_model_ids", [])]
        if role in selected:
            order = [model for model in order if model in selected[role]]
        selected[role] = order
    token = _model_selections.set(selected)
    try:
        yield
    finally:
        _model_selections.reset(token)


def model_order(role):
    return (_model_selections.get() or {}).get(normalized_role(role))


def execution_allowed(model, role):
    order = model_order(role)
    if order is not None and model.id not in order:
        return False
    scope = _execution.get()
    if scope is None:
        return True
    role = normalized_role(role)
    return role in scope["roles"] and (scope["models"] is None or model.id in scope["models"])


def execution_guard(model=None, role=None, tools=None):
    if model is not None and not execution_allowed(model, role):
        raise ExecutionAborted("Execution-local model/role restriction")
    scope = _execution.get()
    if scope is None:
        return
    scope["guard"]()
    if model is not None and not execution_allowed(model, role):
        raise ExecutionAborted("Execution-local model/role restriction")
    if tools is not None and not set(tools) <= scope["tools"]:
        raise ExecutionAborted("Execution-local tool restriction")


def tighter(*values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


def strictest(*modes):
    return min((m for m in modes if m is not None), key=ORDER.get, default="CLOUD_ALLOWED")


def current_contract():
    return _contract.get() or {}


@contextmanager
def contract_scope(
    mode=None,
    budget=None,
    node=None,
    node_tokens=None,
    node_files=None,
    quality=None,
    files=None,
    blocked_files=None,
    repository=None,
):
    old = current_contract()
    budget = budget or EgressBudget()
    data = {
        **old,
        "mode": strictest(old.get("mode"), mode),
        "tokens": tighter(old.get("tokens"), budget.max_cloud_context_tokens_per_request),
        "file_limit": tighter(old.get("file_limit"), budget.max_cloud_files_per_request),
        "quality": max(old.get("quality") or 0, quality or 0),
    }
    for key, value in {
        "node": node,
        "node_tokens": node_tokens,
        "node_files": node_files,
        "files": files,
        "blocked_files": blocked_files,
        "repository": repository,
    }.items():
        if value is not None:
            data[key] = value
    token = _contract.set(data)
    try:
        yield data
    finally:
        _contract.reset(token)


def reserve_egress(store, call, request, model, role, tokens):
    """Count conservative input bounds before send; failures never refund egress."""
    from .privacy import _repository_policy, is_local

    provider = store.settings.providers[model.provider]
    if is_local(provider):
        return
    scope = current_contract()
    repo = _repository_policy.get()
    limits = [store.settings.privacy]
    if repo is not None and repo.privacy is not None:
        limits.append(repo.privacy)
    token_limit = tighter(scope.get("tokens"), *(b.max_cloud_context_tokens_per_request for b in limits))
    file_limit = tighter(scope.get("file_limit"), *(b.max_cloud_files_per_request for b in limits))
    if role == "gateway" and file_limit is not None:
        raise PrivacyViolation(
            "Gateway messages have no verifiable source-file count; use a local model or the native task API with this file budget"
        )
    files = sorted(set(scope.get("files", [])))
    with store.transaction():
        rows = store.db.execute("SELECT node,tokens,files FROM egress WHERE request=?", (request,)).fetchall()

        def check(selected, max_tokens, max_files):
            all_files = set(files)
            for row in selected:
                all_files.update(json.loads(row["files"]))
            if max_tokens is not None and sum(row["tokens"] for row in selected) + tokens > max_tokens:
                raise PrivacyViolation(
                    "Cloud context budget exhausted; use local intelligence or raise the explicit egress budget"
                )
            if max_files is not None and len(all_files) > max_files:
                raise PrivacyViolation("Cloud file budget exhausted")

        check(rows, token_limit, file_limit)
        if scope.get("node"):
            check([r for r in rows if r["node"] == scope["node"]], scope.get("node_tokens"), scope.get("node_files"))
        store.db.execute(
            "INSERT INTO egress VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                call,
                request,
                scope.get("node"),
                model.provider,
                model.id,
                role,
                tokens,
                json.dumps(files),
                "reserved",
                time.time(),
                "conservative_input_token_bound",
            ),
        )


def egress_summary(store, request):
    rows = [dict(r) for r in store.db.execute("SELECT * FROM egress WHERE request=? ORDER BY stamp,call", (request,))]
    for row in rows:
        row["files"] = json.loads(row["files"])
    return {
        "context_tokens_upper_bound": sum(r["tokens"] for r in rows),
        "files": sorted({f for r in rows for f in r["files"]}),
        "calls": rows,
        "measurement": "Conservative input bound, including prompts and generated context; not provider token attestation.",
    }
