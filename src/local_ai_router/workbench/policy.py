"""Explicit transport-derived principals; payloads cannot grant authority."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from ..contracts import strictest
from .schema import ActorType, ReviewPolicy, budget_microusd
from .store import WorkbenchError

CAPABILITIES = frozenset(
    {
        "workspace.read",
        "project.write",
        "prd.write",
        "work.write",
        "comment.write",
        "assignment.write",
        "execution.request",
        "execution.approve",
        "review.approve",
        "device.admin",
        "policy.admin",
        "agent.admin",
        "repository.admin",
        "artifact.read",
    }
)


@dataclass(frozen=True)
class Principal:
    id: str
    actor_type: ActorType
    capabilities: frozenset[str]
    workspaces: frozenset[str]
    local: bool = False

    def authorize(self, workspace_id: str, capability: str, *, local_only: bool = False):
        if workspace_id not in self.workspaces and not (self.local and "*" in self.workspaces):
            raise WorkbenchError("NOT_FOUND", "Workspace not found", 404)
        if capability not in self.capabilities or (local_only and not self.local):
            raise WorkbenchError("FORBIDDEN", "Action is not granted to this principal", 403)


def authorize(principal: Principal | None, workspace_id: str, capability: str, *, local_only: bool = False):
    if not isinstance(principal, Principal):
        raise WorkbenchError("UNAUTHENTICATED", "An explicit principal is required", 401)
    principal.authorize(workspace_id, capability, local_only=local_only)


@dataclass(frozen=True)
class EffectivePolicy:
    privacy: str
    review: ReviewPolicy
    budget_microusd: int
    quality: float


def effective(workspace, project, item, settings, repository=None, sources=()):
    modes = [
        workspace.inference_policy,
        project.default_privacy,
        item.privacy_policy,
        repository.privacy_mode if repository else None,
    ]
    if workspace.privacy_mode in {"DEVICE_ONLY", "PRIVATE_LAN"}:
        modes.append("LOCAL_ONLY")
    modes.extend(source.privacy_policy for source in sources)
    reviews = [workspace.review_policy, project.review_policy, item.review_policy] + [
        source.review_policy for source in sources
    ]
    order = {ReviewPolicy.AUTO_VERIFY: 0, ReviewPolicy.AI_REVIEW: 1, ReviewPolicy.HUMAN_APPROVAL_REQUIRED: 2}
    caps = [budget_microusd(item.budget_limit)]
    if workspace.execution_policy.budget_ceiling is not None:
        caps.append(budget_microusd(workspace.execution_policy.budget_ceiling))
    quality = max(
        item.quality_target or 0,
        settings.routing.quality_slos.get("default", 0),
        *(source.quality_target or 0 for source in sources),
        0,
    )
    return EffectivePolicy(strictest(*modes), max(reviews, key=order.get), min(caps), quality)


def remaining_budget(store, item, cap):
    """Existing provider charges plus unsettled attempt allowance, rounded conservatively."""
    rows = store.db.execute(
        "SELECT a.id,a.request_id,r.cap_microusd,r.state FROM wb_attempts a LEFT JOIN wb_budget_reservations r ON r.attempt_id=a.id WHERE a.workspace_id=? AND a.item_id=?",
        (item.workspace_id, item.id),
    ).fetchall()
    spent, held, requests = 0, 0, set()
    for row in rows:
        cost = 0
        if row["request_id"] and row["request_id"] not in requests:
            requests.add(row["request_id"])
            values = store.db.execute("SELECT cost FROM calls WHERE request=?", (row["request_id"],)).fetchall()
            cost = sum(
                int((Decimal(str(value[0])) * 1_000_000).to_integral_value(rounding=ROUND_CEILING)) for value in values
            )
            spent += cost
        if row["state"] and row["state"] != "SETTLED":
            held += max(0, row["cap_microusd"] - cost)
    return {"remaining_microusd": max(0, cap - spent - held), "spent_microusd": spent, "held_microusd": held}
