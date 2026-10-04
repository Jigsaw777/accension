"""Typed, bounded contracts. No executable instructions are trusted from models."""
from __future__ import annotations
from typing import Literal, Any
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator

def uid() -> str:
    return uuid4().hex

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

class ModelDescriptor(Strict):
    """Canonical inventory record. Flat V1 fields remain accepted at the boundary."""
    id: str
    provider: str
    deployment_name: str
    tier: int = Field(default=1, ge=1, le=4)
    enabled: bool = True
    roles: list[str] = ["executor", "repair", "reviewer", "planner", "gateway"]
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)
    cached_input_price: float | None = Field(default=None, ge=0)
    cache_write_price: float | None = Field(default=None, ge=0)
    context_window: int = Field(default=32768, gt=0)
    max_output: int = Field(default=4096, gt=0)
    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_streaming: bool = False
    supports_prompt_cache: bool = False
    supports_vision: bool = False
    supports_code: bool = True
    supports_reasoning: bool = False
    supports_mcp: bool = False
    supports_responses_api: bool = False
    supports_chat_completions: bool = True
    expected_latency: float = Field(default=5, ge=0)
    concurrency_limit: int = Field(default=1, ge=1, le=32)
    rate_limit: int = Field(default=30, gt=0)
    quality_priors: dict[str, float] = {"default": 0.85}
    status: Literal["available", "degraded", "unavailable", "deprecated", "removed", "unknown"] = "available"
    locality: Literal["local", "cloud", "unknown"] = "unknown"
    protocols: list[str] = []
    supports_text: bool = True
    supports_embeddings: bool = False
    quality: dict[str, float] = {}
    reliability: float = Field(default=1, ge=0, le=1)
    ttft: float | None = Field(default=None, ge=0)
    tokens_per_second: float | None = Field(default=None, ge=0)
    ram_estimate_mb: float | None = Field(default=None, ge=0)
    vram_estimate_mb: float | None = Field(default=None, ge=0)
    loaded: bool | None = None
    local_runtime: str | None = None
    pricing_status: Literal["known", "unknown", "estimated", "user_supplied", "provider_reported"] = "unknown"
    currency: Literal["USD"] = "USD"
    pricing_source: str = "unknown"
    pricing_updated_at: float | None = None
    capabilities_source: str = "user_configuration"
    capability_evidence: dict[str, dict[str, Any]] = {}
    discovered_from: str = "user_configuration"
    discovered_at: float | None = None
    benchmarked_at: float | None = None
    inventory_stale: bool = False

    @model_validator(mode="before")
    @classmethod
    def accept_profile(cls, values):
        if not isinstance(values, dict):
            return values
        data = dict(values)
        if "remote_id" in data:
            data.setdefault("deployment_name", data.pop("remote_id"))
        for key, mapping in {
            "capabilities": {},
            "context": {"input": "context_window", "output": "max_output"},
            "economics": {"status": "pricing_status"},
            "performance": {"latency": "expected_latency"},
            "resource": {"ram_estimate": "ram_estimate_mb", "vram_estimate": "vram_estimate_mb"},
            "provenance": {},
        }.items():
            nested = data.pop(key, {})
            for name, value in nested.items():
                field = "supports_" + name if key == "capabilities" else mapping.get(name, name)
                data.setdefault(field, value)
        if "pricing_status" not in data and data.get("input_price") is not None and data.get("output_price") is not None:
            data["pricing_status"] = "user_supplied"
        return data

    @model_validator(mode="after")
    def valid_priors(self):
        if any(not 0 <= v <= 1 for v in list(self.quality_priors.values()) + list(self.quality.values())):
            raise ValueError("Quality priors must be probabilities")
        if self.supports_prompt_cache and self.cache_write_price is None:
            raise ValueError("Prompt caching requires an explicit cache_write_price")
        return self

    def supports(self, capabilities: list[str]) -> bool:
        return all(bool(getattr(self, "supports_" + c, False)) for c in capabilities)

    def profile(self) -> dict:
        """The V2 shape used by UI/SDK; no competing copy of mutable capabilities."""
        return {
            "id": self.id, "provider": self.provider, "remote_id": self.deployment_name,
            "status": self.status, "locality": self.locality, "protocols": self.protocols,
            "capabilities": {k.removeprefix("supports_"): getattr(self, k) for k in type(self).model_fields if k.startswith("supports_")},
            "context": {"input": self.context_window, "output": self.max_output},
            "economics": {k: getattr(self, k) for k in ("input_price", "output_price", "cached_input_price", "cache_write_price", "currency", "pricing_status", "pricing_source", "pricing_updated_at")},
            "performance": {"latency": self.expected_latency, "ttft": self.ttft, "tokens_per_second": self.tokens_per_second, "reliability": self.reliability},
            "quality": self.quality, "resource": {"ram_estimate": self.ram_estimate_mb, "vram_estimate": self.vram_estimate_mb, "loaded": self.loaded, "local_runtime": self.local_runtime},
            "provenance": {k: getattr(self, k) for k in ("discovered_from", "capabilities_source", "pricing_source", "benchmarked_at", "discovered_at", "inventory_stale")},
        }


# Import compatibility for V1 integrations.
Model = ModelDescriptor

class Provider(Strict):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    endpoint: str = ""
    api_key_env: str = ""
    auth_header: str = "Authorization"
    api: Literal["chat", "responses", "messages"] = "chat"
    enabled: bool = True
    local: bool = False
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    tool: str = "code_task"
    token_command: list[str] = []
    azure_identity: bool = False
    protocol: str = ""
    credential_ref: str = ""
    auth: Literal["auto", "none", "api_key", "azure_identity", "aws_chain", "google_adc", "command"] = "auto"
    region: str = ""
    project: str = ""
    profile: str = ""
    service_account_file: str = ""
    inventory: bool = True
    model_ids: list[str] = []
    groups: list[str] = []
    include_models: list[str] = ["*"]
    exclude_models: list[str] = []
    # An explicitly added private-network service is still remote for privacy policy.
    options: dict[str, str | int | bool] = {}

class Classification(Strict):
    task_family: Literal["simple", "coding", "architecture", "critical", "explanation"] = "coding"
    complexity: int = Field(default=35, ge=0, le=100)
    risk: int = Field(default=20, ge=0, le=100)
    planning_required: bool = False
    estimated_files: int = Field(default=1, ge=1, le=100)
    requires_repo_analysis: bool = True
    requires_frontier_planner: bool = False
    recommended_tier: int = Field(default=1, ge=1, le=4)
    confidence: float = Field(default=0.8, ge=0, le=1)
    reason_codes: list[str] = []
    review_required: bool = False
    required_capabilities: list[str] = ["code"]
    decision_source: list[str] = ["static_analysis"]
    planning_depth: Literal["NONE", "MICRO_PLAN", "IMPLEMENTATION_PLAN", "FULL_ARCHITECTURE_PLAN"] = "MICRO_PLAN"
    signals: dict[str, int | float | str | bool] = {}

class Arbitration(Strict):
    model_id: str | None = None
    recommended_tier: int = Field(default=2, ge=1, le=4)
    planning_required: bool = True
    reason_code: Literal["FRONTIER_REQUIRED", "LOCAL_SUFFICIENT", "CLOUD_CHEAP_SUFFICIENT", "ARCHITECTURE_PLANNER_REQUIRED", "ESCALATE_ONE_TIER", "STOP_ESCALATION", "BUDGET_OVERRIDE_ALLOWED", "NEEDS_HUMAN_APPROVAL"]

class EgressBudget(Strict):
    max_cloud_context_tokens_per_request: int | None = Field(default=None, ge=0)
    max_cloud_files_per_request: int | None = Field(default=None, ge=0)


class TaskNode(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    jira_key: str = ""
    title: str
    objective: str
    detailed_description: str = ""
    rationale: str = ""
    task_type: str = "coding"
    dependencies: list[str] = []
    relevant_files: list[str] = []
    likely_symbols: list[str] = []
    required_context: list[str] = []
    acceptance_criteria: list[str] = Field(min_length=1)
    unit_tests: list[str] = []
    integration_tests: list[str] = []
    validation_commands: list[str] = []
    skills_required: list[str] = []
    tools_required: list[str] = []
    estimated_input_tokens: int = Field(default=2000, ge=0)
    estimated_output_tokens: int = Field(default=2000, ge=0)
    complexity: int = Field(default=30, ge=0, le=100)
    risk: int = Field(default=20, ge=0, le=100)
    preferred_model_tier: int = Field(default=1, ge=1, le=4)
    minimum_capabilities: list[str] = ["code"]
    max_cost: float = Field(default=0.1, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=5)
    parallelizable: bool = True
    expected_artifacts: list[str] = Field(min_length=1)
    verifier_requirements: list[str] = []
    escalation_conditions: list[str] = []
    privacy_class: Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"] = "CLOUD_ALLOWED"
    cloud_eligible: bool = True
    quality_slo: float | None = Field(default=None, ge=0, le=1)
    max_cloud_context: int | None = Field(default=None, ge=0)
    max_cloud_files: int | None = Field(default=None, ge=0)
    fallbacks: list[str] = []

class ExecutionPlan(Strict):
    plan_id: str = Field(default_factory=uid, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    request_id: str = Field(default_factory=uid)
    goal: str
    task_type: str = "coding"
    assumptions: list[str] = []
    constraints: list[str] = []
    architecture_summary: str = ""
    success_criteria: list[str] = Field(min_length=1)
    risk_level: int = Field(default=20, ge=0, le=100)
    estimated_complexity: int = Field(default=30, ge=0, le=100)
    allowed_budget: float = Field(default=0.5, ge=0)
    artifacts: list[str] = []
    tasks: list[TaskNode] = Field(min_length=1, max_length=64)
    dependency_edges: list[tuple[str, str]] = []
    global_validation: list[str] = []
    rollback_strategy: str = "Restore only router-written files from backups, after checking hashes."
    planner_model: str = "deterministic"
    planner_confidence: float = Field(default=0.8, ge=0, le=1)
    privacy_contract: Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"] = "CLOUD_ALLOWED"
    egress_budget: EgressBudget = Field(default_factory=EgressBudget)
    quality_contract: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def check_dag(self):
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate task ids")
        dependencies = {t.id: set(t.dependencies) for t in self.tasks}
        for parent, child in self.dependency_edges:
            if parent not in dependencies or child not in dependencies:
                raise ValueError("Unknown dependency edge")
            dependencies[child].add(parent)
        if any(not deps <= set(ids) for deps in dependencies.values()):
            raise ValueError("Unknown dependency")
        pending = dict(dependencies)
        while pending:
            ready = {i for i, deps in pending.items() if not deps}
            if not ready:
                raise ValueError("Task DAG contains a cycle")
            pending = {i: deps - ready for i, deps in pending.items() if i not in ready}
        for task in self.tasks:
            task.dependencies = sorted(dependencies[task.id])
        return self

class FileChange(Strict):
    path: str
    content: str = Field(max_length=500_000)
    original_sha256: str | None = None

class WorkerResult(Strict):
    status: Literal["complete", "blocked"]
    summary: str
    files_changed: list[FileChange] = Field(default=[], max_length=24)
    tests_added: list[str] = []
    commands_run: list[str] = []
    remaining_risks: list[str] = []
    confidence: float = Field(default=0.5, ge=0, le=1)
    escalation_requested: bool = False

class Review(Strict):
    accepted: bool
    findings: list[str] = []

class Request(Strict):
    task: str = Field(min_length=1, max_length=100000)
    repo_path: str
    constraints: list[str] = []
    budget: float | None = Field(default=None, ge=0)
    execution_mode: Literal["safe-auto", "plan-only"] = "safe-auto"
    session_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    privacy: Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"] | None = None
    quality: float | None = Field(default=None, ge=0, le=1)
    max_cloud_context: int | None = Field(default=None, ge=0)
    max_cloud_files: int | None = Field(default=None, ge=0)

class Usage(Strict):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

class Generation(Strict):
    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    raw: dict[str, Any] = {}
