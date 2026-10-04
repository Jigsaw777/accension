"""Execution-local restrictions and transactional, metadata-only egress accounting."""

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .errors import PrivacyViolation
from .schema import EgressBudget

ORDER = {"LOCAL_ONLY": 0, "CLOUD_REDACTED": 1, "CLOUD_ALLOWED": 2}
_contract = ContextVar("accension_execution_contract", default=None)


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
