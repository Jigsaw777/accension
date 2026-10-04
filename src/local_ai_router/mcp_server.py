"""Compact stdio interface; administrative functions are grouped under router_info."""
import asyncio, json
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from .config import load
from .engine import Engine
from .schema import Request

def create_mcp(settings=None, shared_engine=None):
    settings = settings or load()

    @asynccontextmanager
    async def lifespan(server):
        if shared_engine is not None:
            yield shared_engine
            return
        engine = Engine(settings)
        watcher = asyncio.create_task(engine.discovery.watch())
        try:
            yield engine
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await engine.close()

    mcp = FastMCP("local-ai-router", lifespan=lifespan, stateless_http=True, json_response=True,
        instructions="Companion Mode: the host controls when Accension is invoked. For explicitly delegated repository coding, pass the goal and constraints to orchestrate_feature. It compiles a portable execution graph, selects models, applies bounded edits and runs registered checks. Read the execution receipt for observed evidence. Registered repositories and checks are required.")

    def current_engine():
        if shared_engine is not None:
            return shared_engine
        return mcp.get_context().request_context.lifespan_context

    async def with_engine(method, *args):
        async with asyncio.timeout(settings.routing.timeouts["mcp"]):
            return await getattr(current_engine(), method)(*args)

    @mcp.tool()
    async def orchestrate_feature(task: str, repo_path: str, constraints: list[str] | None = None, budget: float | None = None, execution_mode: str = "safe-auto", privacy: str | None = None, max_cloud_context: int | None = None, quality: float | None = None) -> dict:
        """Autonomous cost-optimized planning and implementation. Give goal and constraints; returns files, tests, costs and risks."""
        return await with_engine("run", Request(task=task, repo_path=repo_path, constraints=constraints or [], budget=budget, execution_mode=execution_mode,
                                                privacy=privacy, max_cloud_context=max_cloud_context, quality=quality))

    @mcp.tool()
    async def orchestrate_bugfix(task: str, repo_path: str, constraints: list[str] | None = None, budget: float | None = None) -> dict:
        """Fix a repository bug with bounded workers and registered regression checks."""
        return await orchestrate_feature(task, repo_path, constraints, budget)

    @mcp.tool()
    async def route_task(task: str, repo_path: str | None = None) -> dict:
        """Preview classification and eligible models without inference or edits."""
        return await with_engine("route", task, None, repo_path)

    @mcp.tool()
    async def plan_task(task: str, repo_path: str, constraints: list[str] | None = None, budget: float | None = None) -> dict:
        """Generate and persist a validated task DAG without editing source files."""
        plan = await with_engine("plan", Request(task=task, repo_path=repo_path, constraints=constraints or [], budget=budget))
        return {"plan_id": plan.plan_id, "request_id": plan.request_id, "tasks": len(plan.tasks), "summary": plan.architecture_summary}

    @mcp.tool()
    async def execute_plan(plan_id: str, repo_path: str) -> dict:
        """Execute a stored plan once, rejecting stale repository content."""
        return await with_engine("execute_plan", plan_id, repo_path)

    @mcp.tool()
    async def receipt(run_id: str) -> dict:
        """Read local execution evidence: models, costs, hashes, checks and cloud egress. No model calls."""
        from .receipts import get
        return get(current_engine(), run_id)

    @mcp.tool()
    async def router_status(run_id: str = "") -> dict:
        """Read a run's status or local hub readiness."""
        if run_id:
            from .task_api import run_status
            return run_status(current_engine(), run_id)
        return await router_info("status")

    @mcp.tool()
    async def explain_route(task: str, repo_path: str | None = None) -> dict:
        """Explain current capability, budget and privacy gates without paid inference."""
        return await route_task(task, repo_path)

    @mcp.tool()
    async def estimate_cost(task: str, repo_path: str | None = None) -> dict:
        """Estimate routing costs from current metadata; never runs shadow inference."""
        result = await route_task(task, repo_path)
        return {"costs": result["cost_simulation"], "economics": result["economics"], "privacy": result["privacy"], "inference_calls": 0}

    @mcp.tool()
    async def router_info(action: str = "status", request_id: str = "", offset: int = 0, limit: int = 50) -> dict:
        """Read status, models, roles, providers, recovery, costs, savings, cache, trace or budget. Does not run paid calibration."""
        engine = current_engine()
        if action == "models":
            return engine.store.models_page(offset=offset, limit=limit)
        if action == "trace":
            return {"trace": engine.store.traces(request_id)}
        if action in ("costs", "budget"):
            return {"costs": engine.store.costs(), "limits": settings.budgets.model_dump(), "savings_today": engine.store.economics("summary", "today")}
        if action == "savings":
            return engine.store.economics("state") or {"available": False}
        if action == "cache":
            return engine.store.cache_stats()
        if action == "roles":
            from .ui import compact_role
            return {k: compact_role(v) for k, v in engine.router.roles.all().items()}
        if action == "receipt":
            return await receipt(request_id)
        if action == "providers":
            from .management import Management
            return {"providers": Management(engine).providers()}
        if action == "recovery":
            from .recovery import list_runs
            return {"runs": list_runs(engine)}
        if action == "status":
            return {"status": "ok", "mock": settings.mock, "models": len(settings.models), "registered_repositories": len(settings.repositories)}
        raise ValueError("Unknown info action")
    return mcp
