from __future__ import annotations
import os, re, secrets, hashlib, json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
import yaml
from pydantic import ConfigDict, Field, model_validator
from .schema import Strict, Model, Provider, EgressBudget
from .migration import LegacyRoutingAccessors, migrate_routing

class Routing(LegacyRoutingAccessors, Strict):
    classifier_confidence_threshold: float = Field(default=.80, ge=0, le=1)
    arbiter_risk_threshold: int = Field(default=70, ge=0, le=100)
    frontier_planner_complexity_threshold: int = Field(default=65, ge=0, le=100)
    max_parallel_workers: int = Field(default=2, ge=1, le=16)
    local_first: bool = True
    classifier_enabled: bool = False
    classifier_command: str = ""
    classifier_args: list[str] = []
    classifier_tool: str = Field(default="classify", min_length=1)
    classifier_timeout: float = Field(default=2, gt=0, le=120)
    arbiter_model: str | None = None
    require_arbiter_for_critical: bool = False
    planner_model_candidates: list[str] = []
    quality_slos: dict[str, float] = {"simple": .85, "coding": .90, "architecture": .94, "critical": .98, "explanation": .85}
    cost_weight: float = .15
    latency_weight: float = .03
    failure_risk_weight: float = .2
    cache_hit_bonus: float = .01
    local_execution_bonus: float = .02
    historical_success_bonus: float = .03
    reviewer_risk_threshold: int = 50
    allow_below_slo: bool = False
    max_attempts: int = 3
    circuit_failures: int = 3
    circuit_cooldown: float = 60
    timeouts: dict[str, float] = {"arbiter": 30, "local": 180, "cloud": 90, "planner": 120, "tests": 120, "mcp": 600}
    token_ceilings: dict[str, int] = {"simple": 4000, "coding": 12000, "architecture": 20000, "critical": 24000, "explanation": 6000}
    preset: Literal["balanced", "maximum-savings", "quality-first", "fully-local", "local-control", "custom"] = "balanced"
    allow_unknown_pricing: bool = False
    unknown_input_price: float = Field(default=20, gt=0)
    unknown_output_price: float = Field(default=100, gt=0)
    price_max_age_days: int = Field(default=30, ge=1)
    allow_stale_pricing: bool = False
    resource_aware: bool = True
    available_ram_mb: float | None = Field(default=None, ge=0)
    max_cost_confirmation: float = Field(default=.5, ge=0)

    @model_validator(mode="before")
    @classmethod
    def migrate(cls, data):
        return migrate_routing(data) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def bounds(self):
        if any(not 0 <= value <= 1 for value in self.quality_slos.values()):
            raise ValueError("Quality SLOs must be probabilities")
        if any(value <= 0 for value in self.timeouts.values()) or any(value < 256 for value in self.token_ceilings.values()):
            raise ValueError("Timeouts and token ceilings must be positive")
        return self


class ControlPlane(Strict):
    routing_location: Literal["local-only", "hybrid"] = "local-only"
    fully_local: bool = False
    telemetry: Literal[False] = False
    mode: Literal["companion", "sovereign"] = "companion"


class RolePolicy(Strict):
    strategy: Literal["auto", "preferred", "pinned", "disabled"] = "auto"
    model: str | None = None
    locality: Literal["local-only", "local-preferred", "any"] = "local-preferred"
    minimum_quality: float | None = Field(default=None, ge=0, le=1)
    allowed_provider_groups: list[str] = []


ROLE_NAMES = ("classifier", "arbiter", "planner", "executor", "fast_executor", "complex_executor", "reviewer", "repairer", "compactor", "summarizer", "embedding", "vision")


class Privacy(EgressBudget):
    mode: Literal["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"] = "LOCAL_ONLY"
    never_send: list[str] = []

    @model_validator(mode="after")
    def relative_patterns(self):
        if any(p.startswith(("/", "\\")) or ".." in p.replace("\\", "/").split("/") or ":" in p for p in self.never_send):
            raise ValueError("Sensitive path patterns must be repository-relative")
        return self


class Plugins(Strict):
    enabled: list[str] = []


class Calibration(Strict):
    budget: float = Field(default=.05, ge=0, le=10)
    max_models: int = Field(default=3, ge=1, le=10)
    max_cases: int = Field(default=11, ge=1, le=20)


class Runtime(Strict):
    discovery_concurrency: int = Field(default=8, ge=1, le=256)
    health_probe_concurrency: int = Field(default=8, ge=1, le=256)
    inference_concurrency: int = Field(default=8, ge=1, le=256)
    calibration_concurrency: int = Field(default=2, ge=1, le=64)
    discovery_timeout: float = Field(default=120, gt=0, le=3600)

class Savings(Strict):
    enabled: bool = True
    baseline_method: Literal["DIRECT_MODEL", "HOST_MODEL", "USER_SELECTED_MODEL", "QUALITY_BASELINE", "DISABLED"] = "DIRECT_MODEL"
    baseline_model: str | None = None
    header_period: Literal["current", "session", "today", "7d", "30d", "all"] = "today"
    show_tokens: bool = True
    show_percentage: bool = True
    currency: Literal["USD"] = "USD"

class Budgets(Strict):
    default_request_budget: float = Field(default=.5, ge=0)
    session_budget: float = Field(default=3, ge=0)
    daily_soft_budget: float = Field(default=3, ge=0)
    daily_hard_budget: float = Field(default=5, ge=0)
    planner_budget: float = Field(default=.2, ge=0)
    worker_budget: float = Field(default=.25, ge=0)
    verification_budget: float = Field(default=.05, ge=0)
    max_frontier_calls: int = Field(default=2, ge=0)
    max_initial_frontier_plans: int = Field(default=1, ge=0)

class CacheConfig(Strict):
    enabled: bool = True
    ttl_seconds: int = Field(default=86400, gt=0)
    max_entries: int = Field(default=1000, gt=0)
    lru_entries: int = Field(default=128, gt=0)
    semantic_cache_enabled: bool = False

    @model_validator(mode="after")
    def exact_only(self):
        if self.semantic_cache_enabled:
            raise ValueError("Semantic cache is not implemented; use exact caching")
        return self

class Repository(Strict):
    path: str
    validation: dict[str, list[str]] = {}
    allow_cloud: bool = False
    max_file_bytes: int = 200000
    max_context_files: int = 12
    privacy: Privacy | None = None

    @property
    def cloud_allowed(self):
        return self.privacy.mode != "LOCAL_ONLY" if self.privacy is not None else self.allow_cloud

    @property
    def privacy_mode(self):
        return self.privacy.mode if self.privacy is not None else "CLOUD_ALLOWED" if self.allow_cloud else "LOCAL_ONLY"

class Discovery(Strict):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=30)
    azure_subscription: str = ""
    azure_resource_group: str = ""
    azure_account: str = ""
    azure_tenant: str = "organizations"
    management_token_env: str = "AZURE_MANAGEMENT_TOKEN"
    token_command: list[str] = []
    # Generic HTTP /models is accepted only for explicitly opted-in deployment inventories.
    inventory_providers: list[str] = []
    model_defaults: dict = {"tier": 2, "quality_priors": {"default": 0.85}}
    overrides: dict[str, dict] = {}
    local_scan: bool = True
    local_endpoints: dict[str, str] = {"ollama": "http://127.0.0.1:11434", "lmstudio": "http://127.0.0.1:1234/v1", "local-openai": "http://127.0.0.1:8080/v1", "vllm": "http://127.0.0.1:8000/v1"}
    probe_timeout: float = Field(default=.5, gt=0, le=10)

class Settings(Strict):
    # Runtime discovery updates providers and models together; management writes
    # validate a complete replacement Settings before committing it.
    model_config = ConfigDict(extra="forbid", validate_assignment=False, allow_inf_nan=False)
    schema_version: Literal[2] = 2
    home: Path
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    providers: dict[str, Provider] = {}
    models: list[Model] = []
    routing: Routing = Field(default_factory=Routing)
    budgets: Budgets = Field(default_factory=Budgets)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    repositories: list[Repository] = []
    skills: dict[str, str] = {}
    tool_registry: dict[str, dict] = {}
    discovery: Discovery = Field(default_factory=Discovery)
    mock: bool = False
    control_plane: ControlPlane = Field(default_factory=ControlPlane)
    roles: dict[str, RolePolicy] = Field(default_factory=lambda: {name: RolePolicy(locality="local-only" if name in ("classifier", "arbiter") else "local-preferred") for name in ROLE_NAMES})
    plugins: Plugins = Field(default_factory=Plugins)
    calibration: Calibration = Field(default_factory=Calibration)
    runtime: Runtime = Field(default_factory=Runtime)
    privacy: EgressBudget = Field(default_factory=EgressBudget)
    savings: Savings = Field(default_factory=Savings)

    @model_validator(mode="before")
    @classmethod
    def version(cls, data):
        if isinstance(data, dict) and data.get("schema_version") == 1:
            data = {**data, "schema_version": 2}
        return data

    @model_validator(mode="after")
    def valid_boundaries(self):
        if self.host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Accension must bind to a loopback host")
        if any(name not in ROLE_NAMES for name in self.roles):
            raise ValueError("Unknown role policy")
        ids = [m.id for m in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate model ids")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) for name in self.providers):
            raise ValueError("Provider instance IDs must use letters, digits, dots, hyphens or underscores")
        for model in self.models:
            if model.provider not in self.providers:
                raise ValueError("Unknown provider for " + model.id)
        for provider in self.providers.values():
            validate_endpoint(provider, self.port)
        for endpoint in self.discovery.local_endpoints.values():
            validate_endpoint(Provider(kind="openai", endpoint=endpoint, local=True), self.port)
        return self

    @property
    def state(self) -> Path:
        from .safety import safe_path
        p = safe_path(self.home, ".router", internal=True)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def token(self) -> str:
        if os.getenv("ROUTER_API_TOKEN"):
            return os.environ["ROUTER_API_TOKEN"]
        p = self.state / "api-token"
        if not p.exists():
            try:
                with p.open("x", encoding="utf-8") as f:
                    f.write(secrets.token_urlsafe(32))
                p.chmod(0o600)
            except FileExistsError:
                pass
        return p.read_text().strip()

    def fingerprint(self) -> str:
        # Exclude secret values; provider config only holds environment variable names.
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def repository(self, path: str) -> Repository:
        resolved = Path(path).resolve(strict=True)
        for repo in self.repositories:
            if resolved == Path(repo.path).resolve():
                return repo
        raise ValueError("Repository is not registered. Use router repo add PATH first.")

def default_home() -> Path:
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "config").is_dir() and (checkout / "pyproject.toml").is_file():
        return checkout
    return Path(os.getenv("APPDATA", str(Path.home() / ".config"))) / "accension"


def validate_endpoint(provider: Provider, port: int):
    if not provider.endpoint:
        return
    u = urlparse(provider.endpoint)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("Provider endpoint must be HTTP(S), without credentials/query/fragment")
    loopback = u.hostname in ("localhost", "127.0.0.1", "::1")
    if u.hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0") and u.port == port:
        raise ValueError("Recursion: downstream points to this gateway")
    if provider.local and not loopback:
        raise ValueError("Local provider must use a loopback endpoint")
    if not loopback and u.scheme != "https":
        raise ValueError("Remote provider endpoints require HTTPS")


def load(home: str | Path | None = None, mock: bool = False) -> Settings:
    root = Path(home or os.getenv("ROUTER_HOME") or default_home()).resolve()
    data: dict = {"home": root, "mock": mock}
    for name in ("version", "providers", "models", "routing", "budgets", "cache", "skills", "repositories", "discovery", "roles", "control_plane", "runtime", "privacy", "savings", "local"):
        path = root / "config" / f"{name}.yaml"
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            # Substitute before YAML parsing with JSON quoting; never inject YAML syntax.
            raw = re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', lambda m: json.dumps(os.getenv(m[1], "")), raw)
            data.update(yaml.safe_load(raw) or {})
    settings = Settings.model_validate(data)
    if mock:
        settings.providers = {"mock": Provider(kind="mock", local=True)}
        settings.models = [Model(id="mock-worker", provider="mock", deployment_name="mock-worker", input_price=0, output_price=0, supports_tools=True, supports_structured_output=True, supports_streaming=True, supports_responses_api=True, quality_priors={"default": .99}, concurrency_limit=4)]
        settings.routing.classifier_enabled = False
        settings.routing.require_arbiter_for_critical = False
    return settings
