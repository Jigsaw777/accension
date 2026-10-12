"""Strict Workbench requests and records, separate from AXIR execution identity."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer, TypeAdapter, model_validator

from ..dag import normalize_dag
from ..safety import FORBIDDEN_PARTS, SENSITIVE
from ..schema import Strict

UUID = Annotated[
    str, Field(strict=True, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
]


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must not be blank")
    return value


Name = Annotated[str, Field(strict=True, min_length=1, max_length=200), AfterValidator(nonblank)]
Prose = Annotated[str, Field(strict=True, max_length=100000)]
Entry = Annotated[str, Field(strict=True, min_length=1, max_length=2000), AfterValidator(nonblank)]
Entries = Annotated[list[Entry], Field(max_length=100)]
Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Identifiers = Annotated[list[Identifier], Field(max_length=100)]
Hash = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
ProjectKey = Annotated[str, Field(strict=True, pattern=r"^[A-Z][A-Z0-9]{1,15}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]


def parse_timestamp(value):
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z", value):
            raise ValueError("Timestamp must be ISO 8601 UTC with millisecond precision")
        elapsed = datetime.fromisoformat(value.replace("Z", "+00:00")) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return elapsed.days * 86400000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
    return value


def json_timestamp(value):
    seconds, milliseconds = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


Timestamp = Annotated[
    int,
    BeforeValidator(parse_timestamp),
    Field(strict=True, ge=0, le=253402300799999),
    PlainSerializer(json_timestamp, when_used="json"),
]
Priority = Annotated[int, Field(strict=True, ge=0, le=4)]
Score = Annotated[int, Field(strict=True, ge=0, le=100)]
Quality = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Attempts = Annotated[int, Field(strict=True, ge=1, le=5)]


def validate_usd(value: str) -> str:
    if len(value) > 64 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError("Budget must be a nonnegative finite decimal USD string")
    with localcontext() as context:
        context.prec = 80
        if Decimal(value) * 1000000 > 2**63 - 1:
            raise ValueError("Budget exceeds SQLite micro-USD storage range")
    return value


USD = Annotated[str, Field(strict=True, max_length=64), AfterValidator(validate_usd)]


def budget_microusd(value: str) -> int:
    # Caps round down; a sub-micro budget never grants extra spending.
    with localcontext() as context:
        context.prec = 80
        return int((Decimal(validate_usd(value)) * 1000000).to_integral_value(rounding=ROUND_FLOOR))


def relative_path(value: str) -> str:
    win = PureWindowsPath(value)
    parts = value.replace("\\", "/").split("/")
    if (
        win.drive
        or win.root
        or any(
            part in {"", ".", ".."}
            or any(char in part for char in ':*?[]<>|"')
            or any(ord(char) < 32 for char in part)
            or part.endswith((" ", "."))
            or PureWindowsPath(part).is_reserved()
            for part in parts
        )
    ):
        raise ValueError("Unsafe exact relative file path")
    if any(part.lower() in FORBIDDEN_PARTS for part in parts) or SENSITIVE.search(value):
        raise ValueError("Protected path")
    return "/".join(parts)


RelativePath = Annotated[str, Field(strict=True, min_length=1, max_length=2000), AfterValidator(relative_path)]
Paths = Annotated[list[RelativePath], Field(max_length=100)]
InferencePolicy = Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"]


class PrivacyMode(StrEnum):
    DEVICE_ONLY = "DEVICE_ONLY"
    PRIVATE_LAN = "PRIVATE_LAN"
    HYBRID = "HYBRID"


class ReviewPolicy(StrEnum):
    AUTO_VERIFY = "AUTO_VERIFY"
    AI_REVIEW = "AI_REVIEW"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"


class WorkItemStatus(StrEnum):
    BACKLOG = "BACKLOG"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEW = "REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    CANCELLED = "CANCELLED"


class WorkItemType(StrEnum):
    FEATURE = "FEATURE"
    BUG = "BUG"
    TASK = "TASK"
    RESEARCH = "RESEARCH"
    TEST = "TEST"
    DOCUMENTATION = "DOCUMENTATION"
    REVIEW = "REVIEW"


class ExecutionKind(StrEnum):
    ENGINE = "ENGINE"
    MANUAL = "MANUAL"


class PRDStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"


class ActorType(StrEnum):
    HUMAN = "HUMAN"
    MACHINE = "MACHINE"
    SYSTEM = "SYSTEM"


class ExecutionPolicy(Strict):
    auto_dispatch: bool = False
    max_active_runs: Annotated[int, Field(strict=True, ge=1)] = 1
    budget_ceiling: USD | None = None
    require_execution_approval: bool = True
    capabilities: Identifiers = []


class WorkspaceFields(Strict):
    name: Name
    description: Prose = ""
    privacy_mode: PrivacyMode = PrivacyMode.DEVICE_ONLY
    inference_policy: InferencePolicy = "LOCAL_ONLY"
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    review_policy: ReviewPolicy = ReviewPolicy.HUMAN_APPROVAL_REQUIRED


class CreateWorkspace(WorkspaceFields):
    command_id: UUID


class Audit(Strict):
    id: UUID
    revision: Revision
    created_at: Timestamp
    updated_at: Timestamp
    created_by: Identifier
    updated_by: Identifier


class Workspace(WorkspaceFields, Audit):
    archived_at: Timestamp | None = None


class ProjectFields(Strict):
    key: ProjectKey
    name: Name
    description: Prose = ""
    repository_ref: Identifier | None = None
    default_privacy: InferencePolicy = "LOCAL_ONLY"
    default_priority: Priority = 2
    max_active_runs: Annotated[int, Field(strict=True, ge=1)] = 1
    review_policy: ReviewPolicy = ReviewPolicy.AUTO_VERIFY


class CreateProject(ProjectFields):
    command_id: UUID
    workspace_id: UUID


class Project(ProjectFields, Audit):
    workspace_id: UUID
    archived_at: Timestamp | None = None


class Requirement(Strict):
    id: Identifier
    text: Entry


class Risk(Strict):
    description: Entry
    impact: Entry
    mitigation: Entry


class Milestone(Strict):
    title: Name
    requirement_ids: Identifiers = []


class PRDSpec(Strict):
    problem_statement: Prose = ""
    background: Prose = ""
    objectives: Entries = []
    scope: Entries = []
    out_of_scope: Entries = []
    functional_requirements: Annotated[list[Requirement], Field(max_length=100)] = []
    nonfunctional_requirements: Annotated[list[Requirement], Field(max_length=100)] = []
    constraints: Entries = []
    assumptions: Entries = []
    dependencies: Entries = []
    risks: Annotated[list[Risk], Field(max_length=100)] = []
    acceptance_criteria: Annotated[list[Requirement], Field(max_length=100)] = []
    architecture_notes: Prose = ""
    milestones: Annotated[list[Milestone], Field(max_length=100)] = []

    @model_validator(mode="after")
    def stable_ids(self):
        ids = [
            entry.id
            for entry in self.functional_requirements + self.nonfunctional_requirements + self.acceptance_criteria
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate PRD requirement/criterion ids")
        return self

    def require_complete(self):
        if (
            not self.problem_statement.strip()
            or not self.objectives
            or not self.scope
            or not self.functional_requirements
            or not self.acceptance_criteria
        ):
            raise ValueError("PRD requires problem, objectives, scope, requirements and acceptance criteria")


class PRDFields(Strict):
    title: Name
    template_id: Identifier | None = None
    template_version: Revision | None = None
    spec: PRDSpec = Field(default_factory=PRDSpec)


class CreatePRD(PRDFields):
    command_id: UUID
    workspace_id: UUID
    project_id: UUID


class PRD(PRDFields, Audit):
    workspace_id: UUID
    project_id: UUID
    template_hash: Hash | None = None
    status: PRDStatus

    @model_validator(mode="after")
    def approved_contract(self):
        if self.status in {PRDStatus.IN_REVIEW, PRDStatus.APPROVED}:
            self.spec.require_complete()
        return self


class ManualProvenance(Strict):
    kind: Literal["manual"] = "manual"


class PlanProvenance(Strict):
    kind: Literal["plan"]
    import_id: UUID
    source_plan_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    source_node_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    source_hash: Hash


class GitReference(Strict):
    kind: Literal["branch", "commit", "worktree", "pr"]
    reference: Entry


class WorkItemSpec(Strict):
    objective: Prose = ""
    description: Prose = ""
    rationale: Prose = ""
    acceptance_criteria: Entries = []
    definition_of_done: Entries = []
    relevant_files: Paths = []
    allowed_paths: Paths = []
    expected_artifacts: Paths = []
    required_capabilities: Identifiers = []
    required_tools: Identifiers = []
    required_skills: Identifiers = []
    validation_checks: Identifiers = []
    verifier_requirements: Entries = []
    constraints: Entries = []
    context_notes: Prose = ""
    requirement_ids: Identifiers = []
    source_provenance: Annotated[ManualProvenance | PlanProvenance, Field(discriminator="kind")] = Field(
        default_factory=ManualProvenance
    )
    git_references: Annotated[list[GitReference], Field(max_length=100)] = []

    @model_validator(mode="after")
    def artifact_scope(self):
        if not set(self.expected_artifacts) <= set(self.allowed_paths):
            raise ValueError("Expected artifacts must be within allowed_paths")
        return self

    def require_ready(self, execution_kind: ExecutionKind, repository_ref: str | None):
        if not self.objective.strip() or not self.acceptance_criteria or not self.definition_of_done:
            raise ValueError("READY requires objective, acceptance criteria and definition of done")
        if execution_kind == ExecutionKind.ENGINE and (
            not repository_ref or not self.allowed_paths or not self.expected_artifacts or not self.validation_checks
        ):
            raise ValueError("ENGINE READY requires registered repository, paths, artifacts and independent validation")


class WorkItemFields(Strict):
    title: Name
    type: WorkItemType = WorkItemType.TASK
    priority: Priority = 2
    risk: Score = 20
    complexity: Score = 30
    prd_id: UUID | None = None
    parent_item_id: UUID | None = None
    preferred_agent_role: Identifier | None = None
    preferred_model_id: Identifier | None = None
    execution_kind: ExecutionKind
    privacy_policy: InferencePolicy = "LOCAL_ONLY"
    budget_limit: USD = "0.10"
    max_attempts: Attempts = 3
    engine_max_attempts: Attempts = 3
    quality_target: Quality | None = None
    review_policy: ReviewPolicy = ReviewPolicy.HUMAN_APPROVAL_REQUIRED
    spec: WorkItemSpec = Field(default_factory=WorkItemSpec)


class CreateWorkItem(WorkItemFields):
    command_id: UUID
    workspace_id: UUID
    project_id: UUID


class WorkItem(WorkItemFields, Audit):
    workspace_id: UUID
    project_id: UUID
    number: Annotated[int, Field(strict=True, ge=1)]
    display_key: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{1,15}-[1-9][0-9]*$")]
    status: WorkItemStatus
    contract_revision: Revision
    assigned_agent_id: UUID | None = None
    reason_code: Identifier | None = None
    resume_state: WorkItemStatus | None = None
    active_attempt_id: UUID | None = None
    archived_at: Timestamp | None = None

    @model_validator(mode="after")
    def display_number(self):
        if int(self.display_key.rsplit("-", 1)[1]) != self.number:
            raise ValueError("Display key number must match item number")
        return self


class Mutation(Strict):
    command_id: UUID
    expected_revision: Revision


class Update(Mutation):
    @model_validator(mode="after")
    def nonnull_fields(self):
        nullable = {
            "repository_ref",
            "prd_id",
            "parent_item_id",
            "preferred_agent_role",
            "preferred_model_id",
            "quality_target",
            "template_id",
            "template_version",
        }
        for field in self.model_fields_set - nullable:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class UpdateWorkspace(Update):
    name: Name | None = None
    description: Prose | None = None
    privacy_mode: PrivacyMode | None = None
    inference_policy: InferencePolicy | None = None
    execution_policy: ExecutionPolicy | None = None
    review_policy: ReviewPolicy | None = None


class UpdateProject(Update):
    key: ProjectKey | None = None
    name: Name | None = None
    description: Prose | None = None
    repository_ref: Identifier | None = None
    default_privacy: InferencePolicy | None = None
    default_priority: Priority | None = None
    max_active_runs: Annotated[int | None, Field(strict=True, ge=1)] = None
    review_policy: ReviewPolicy | None = None


class UpdatePRD(Update):
    title: Name | None = None
    template_id: Identifier | None = None
    template_version: Revision | None = None
    spec: PRDSpec | None = None


class PRDCommand(Mutation):
    command: Literal["submit_review", "approve", "archive"]


class DependencyEdit(Mutation):
    add: Annotated[list[UUID], Field(max_length=100)] = []
    remove: Annotated[list[UUID], Field(max_length=100)] = []

    @model_validator(mode="after")
    def distinct(self):
        if not self.add and not self.remove:
            raise ValueError("Dependency edit requires an edge change")
        if (
            len(set(self.add)) != len(self.add)
            or len(set(self.remove)) != len(self.remove)
            or set(self.add) & set(self.remove)
        ):
            raise ValueError("Duplicate/conflicting dependency change")
        return self


class UpdateWorkItem(Update):
    title: Name | None = None
    type: WorkItemType | None = None
    priority: Priority | None = None
    risk: Score | None = None
    complexity: Score | None = None
    prd_id: UUID | None = None
    parent_item_id: UUID | None = None
    preferred_agent_role: Identifier | None = None
    preferred_model_id: Identifier | None = None
    execution_kind: ExecutionKind | None = None
    privacy_policy: InferencePolicy | None = None
    budget_limit: USD | None = None
    max_attempts: Attempts | None = None
    engine_max_attempts: Attempts | None = None
    quality_target: Quality | None = None
    review_policy: ReviewPolicy | None = None
    spec: WorkItemSpec | None = None


class BasicCommand(Mutation):
    command: Literal["ready", "unblock", "unassign", "request_execution", "cancel", "retry", "request_review"]


class ReasonCommand(Mutation):
    command: Literal["reopen"]
    reason: Entry


class BlockCommand(Mutation):
    command: Literal["block"]
    reason_code: Literal[
        "DEPENDENCY_NOT_DONE",
        "AGENT_DISABLED",
        "AGENT_OFFLINE",
        "NO_CAPACITY",
        "REPOSITORY_DENIED",
        "TOOL_UNAVAILABLE",
        "SKILL_UNTRUSTED",
        "PRIVACY_POLICY",
        "NO_ELIGIBLE_MODEL",
        "NO_ELIGIBLE_REVIEWER",
        "QUALITY_FLOOR",
        "CONTEXT_LIMIT",
        "BUDGET_EXHAUSTED",
        "EXECUTION_APPROVAL_REQUIRED",
        "RECOVERY_REQUIRED",
        "MANUAL_BLOCK",
    ]
    reason: Entry


class ExecutionApproval(Mutation):
    command: Literal["approve_execution"]
    contract_revision: Revision
    contract_hash: Hash
    policy_revision_hash: Hash
    reason: Entry


class CompletionDecision(Mutation):
    command: Literal["approve_completion", "reject_completion"]
    evidence_hash: Hash
    contract_revision: Revision
    reason: Entry


class ManualEvidence(Strict):
    checklist: Annotated[list[Entry], Field(min_length=1, max_length=100)]
    references: Annotated[list[Entry], Field(min_length=1, max_length=100)]


class ManualSubmit(Mutation):
    command: Literal["manual_submit"]
    evidence: ManualEvidence


WorkItemCommand = Annotated[
    BasicCommand | ReasonCommand | BlockCommand | ExecutionApproval | CompletionDecision | ManualSubmit,
    Field(discriminator="command"),
]
command_adapter = TypeAdapter(WorkItemCommand)


class CommentFields(Strict):
    body: Annotated[str, Field(strict=True, min_length=1, max_length=10000)]


class CreateComment(CommentFields):
    command_id: UUID
    workspace_id: UUID
    item_id: UUID


class Comment(CommentFields, Audit):
    workspace_id: UUID
    item_id: UUID
    deleted_at: Timestamp | None = None


class UpdateComment(CommentFields, Mutation):
    pass


class PlanningInput(Strict):
    workspace_id: UUID
    project_id: UUID
    kind: Literal["generate", "decompose"] = "generate"
    requirement: Prose = ""
    title: Name = "Generated PRD"
    constraints: Entries = []
    budget: USD = "0.10"
    prd_id: UUID | None = None
    expected_revision: Revision | None = None
    template_id: Identifier = "prd-default"
    template_version: Revision = 1
    allowed_paths: Paths = []
    requirement_groups: Annotated[list[Identifiers], Field(max_length=100)] = []


class PlanningCommand(PlanningInput):
    command_id: UUID
    quote: Annotated[str, Field(max_length=4000)]


class ImportPreview(Strict):
    command_id: UUID
    workspace_id: UUID
    project_id: UUID
    prd_id: UUID
    prd_revision: Revision
    operation_id: UUID | None = None
    plans: Annotated[list[dict], Field(max_length=32)] = []
    allowed_paths: Paths = []
    requirement_groups: Annotated[list[Identifiers], Field(max_length=32)] = []
    cross_batch_edges: Annotated[list[tuple[Identifier, Identifier]], Field(max_length=1000)] = []


class ImportCommit(Mutation):
    source_hash: Hash
    actions: Annotated[
        dict[Identifier, Literal["create", "update", "keep", "archive", "successor"]], Field(max_length=2000)
    ] = {}
    item_revisions: Annotated[dict[UUID, Revision], Field(max_length=2000)] = {}


class Operation(Strict):
    id: UUID
    workspace_id: UUID
    kind: Literal["generate", "decompose", "reconcile", "team_lead", "recovery", "manual", "retention"]
    subject_id: UUID | None
    state: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "UNCERTAIN"]
    source_revision: Revision | None
    request_hash: Hash
    result: dict | None = None
    error_code: Identifier | None = None
    created_at: Timestamp
    updated_at: Timestamp
    revision: Revision


class PlanImport(Strict):
    id: UUID
    workspace_id: UUID
    project_id: UUID
    prd_id: UUID
    prd_revision: Revision
    source_hash: Hash
    operation_id: UUID | None
    status: Literal["PREVIEW", "COMMITTED"]
    proposal: dict
    revision: Revision
    created_at: Timestamp


class AgentFields(Strict):
    name: Name
    role: Identifier = "engineer"
    adapter_kind: Literal["engine"] = "engine"
    enabled: bool = True
    capabilities: Identifiers = ["code", "text", "structured_output"]
    tools: Identifiers = ["propose_file_changes"]
    skills: Identifiers = []
    repository_permissions: Annotated[list[Hash], Field(max_length=100)] = []
    allowed_workspaces: Annotated[list[UUID], Field(min_length=1, max_length=100)]
    allowed_projects: Annotated[list[UUID], Field(max_length=100)] = []
    supported_model_roles: Annotated[list[Literal["executor", "repairer", "reviewer"]], Field(max_length=3)] = [
        "executor",
        "repairer",
        "reviewer",
    ]
    privacy_policy: InferencePolicy = "LOCAL_ONLY"
    concurrency_limit: Annotated[int, Field(strict=True, ge=1, le=16)] = 1


class AgentDefinition(AgentFields, Audit):
    pass


class AgentCommand(AgentFields):
    command_id: UUID
    expected_revision: Revision | None = None


class AgentProjection(AgentDefinition):
    workspace_id: UUID


class AssignmentCommand(Mutation):
    agent_id: UUID
    policy_revision_hash: Hash


class TeamLeadAdvice(Strict):
    recommended_agent_id: UUID | None = None
    action: Literal["keep", "assign", "split", "inspect"] = "keep"
    risk: Score
    complexity: Score
    explanation: Prose


class TeamLeadCommand(Mutation):
    workspace_id: UUID
    item_id: UUID
    budget: USD = "0.10"


class WorkExecutionContract(Strict):
    model_config = {"extra": "forbid", "validate_default": True, "frozen": True}
    workspace_id: UUID
    project_id: UUID
    item_id: UUID
    attempt_id: UUID
    plan_id: Identifier
    request_id: Identifier
    item_contract_revision: Revision
    assignment_id: UUID
    agent_definition_id: UUID
    agent_definition_revision: Revision
    policy_revision_hash: Hash
    repository_ref: Hash
    title: Name
    spec: WorkItemSpec
    privacy: InferencePolicy
    budget_microusd: Annotated[int, Field(strict=True, ge=0)]
    engine_max_attempts: Attempts
    quality: Quality
    review_policy: ReviewPolicy
    model_roles: Annotated[list[Literal["executor", "repairer", "reviewer"]], Field(max_length=3)]
    tools: Identifiers
    skill_hashes: Annotated[dict[Identifier, Hash], Field(max_length=100)] = {}
    grant_id: UUID | None
    risk: Score
    complexity: Score
    dependency_evidence: Annotated[list[dict], Field(max_length=1000)] = []


class DeviceProjection(Strict):
    id: UUID
    workspace_id: UUID
    revision: Revision
    name: Name
    state: Identifier


class WorkbenchEvent(Strict):
    sequence: Annotated[int, Field(strict=True, ge=1)]
    event_id: UUID
    workspace_id: UUID
    entity_type: Literal[
        "workspace", "project", "prd", "work_item", "comment", "operation", "plan_import", "agent", "device"
    ]
    entity_id: UUID
    entity_revision: Revision
    event_type: Identifier
    actor_type: ActorType
    actor_id: Identifier
    timestamp: Timestamp
    schema_version: Literal[1] = 1
    # Payloads are typed entity projections; credentials/command authority cannot enter them.
    payload: (
        Workspace | Project | PRD | WorkItem | Comment | Operation | PlanImport | AgentProjection | DeviceProjection
    )

    @model_validator(mode="after")
    def entity_projection(self):
        expected = {
            "workspace": Workspace,
            "project": Project,
            "prd": PRD,
            "work_item": WorkItem,
            "comment": Comment,
            "operation": Operation,
            "plan_import": PlanImport,
            "agent": AgentProjection,
            "device": DeviceProjection,
        }[self.entity_type]
        if (
            not isinstance(self.payload, expected)
            or self.payload.id != self.entity_id
            or self.payload.revision != self.entity_revision
        ):
            raise ValueError("Event projection must match entity type, identity and revision")
        workspace_id = self.payload.id if isinstance(self.payload, Workspace) else self.payload.workspace_id
        if workspace_id != self.workspace_id:
            raise ValueError("Event projection workspace mismatch")
        return self


def validate_workbench_graph(ids: list[str], edges: list[tuple[str, str]]) -> dict[str, list[str]]:
    return normalize_dag(ids, edges, reject_duplicate_edges=True)


def validate_parents(parents: dict[str, str | None]) -> None:
    for item, parent in parents.items():
        depth, seen = 0, {item}
        while parent is not None:
            if parent not in parents:
                raise ValueError("Unknown parent")
            if parent in seen:
                raise ValueError("Parent graph contains a cycle")
            seen.add(parent)
            depth += 1
            if depth > 32:
                raise ValueError("Parent depth exceeds 32")
            parent = parents[parent]
