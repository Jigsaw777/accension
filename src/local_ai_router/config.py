from __future__ import annotations
import os, re, secrets, hashlib, json
from pathlib import Path
from urllib.parse import urlparse
import yaml
from pydantic import Field, model_validator
from .schema import Strict, Model, Provider

class Routing(Strict):
    laya_confidence_threshold: float = Field(default=.80, ge=0, le=1)
    jev_risk_threshold: int = Field(default=70, ge=0, le=100)
    frontier_planner_complexity_threshold: int = Field(default=65, ge=0, le=100)
    max_parallel_workers: int = Field(default=2, ge=1, le=16)
    local_first: bool = True
    laya_enabled: bool = True
    laya_command: str = ""
    laya_args: list[str] = []
    classifier_tool: str = Field(default="laya_decide", min_length=1)
    laya_timeout: float = 2
    jev_model: str | None = None
    require_jev_for_critical: bool = True
    planner_model_candidates: list[str] = []
    quality_slos: dict[str, float] = {"simple": .85, "coding": .92, "architecture": .96, "critical": .98, "explanation": .85}
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
            raise ValueError("Semantic cache is not implemented in V1; use exact caching")
        return self

class Repository(Strict):
    path: str
    validation: dict[str, list[str]] = {}
    allow_cloud: bool = False
    max_file_bytes: int = 200000
    max_context_files: int = 12

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

class Settings(Strict):
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

def load(home: str | Path | None = None, mock: bool = False) -> Settings:
    root = Path(home or os.getenv("ROUTER_HOME") or Path(__file__).resolve().parents[2]).resolve()
    data: dict = {"home": root, "mock": mock}
    for name in ("providers", "models", "routing", "budgets", "cache", "skills", "repositories", "discovery", "local"):
        path = root / "config" / f"{name}.yaml"
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            # Substitute before YAML parsing with JSON quoting; never inject YAML syntax.
            raw = re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', lambda m: json.dumps(os.getenv(m[1], "")), raw)
            data.update(yaml.safe_load(raw) or {})
    settings = Settings.model_validate(data)
    ids = [m.id for m in settings.models]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate model ids")
    for model in settings.models:
        if model.provider not in settings.providers:
            raise ValueError(f"Unknown provider for {model.id}")
    for provider in settings.providers.values():
        if provider.endpoint:
            u = urlparse(provider.endpoint)
            if u.scheme not in ("http", "https") or u.username or u.password or u.query:
                raise ValueError("Provider endpoint must be HTTP(S), without credentials/query")
            if u.hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0") and u.port == settings.port:
                raise ValueError("Recursion: downstream points to this gateway")
            if provider.local and u.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Local provider must use a loopback endpoint")
    if mock:
        settings.providers = {"mock": Provider(kind="mock", local=True)}
        settings.models = [Model(id="mock-worker", provider="mock", deployment_name="mock-worker", input_price=0, output_price=0, supports_tools=True, supports_structured_output=True, supports_streaming=True, supports_responses_api=True, quality_priors={"default": .99}, concurrency_limit=4)]
        settings.routing.laya_enabled = False
        settings.routing.require_jev_for_critical = False
    return settings
