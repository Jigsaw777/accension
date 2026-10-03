"""Compact stdio interface; administrative functions are grouped under router_info."""
import asyncio, json
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from .config import load
from .engine import Engine
from .schema import Request

def create_mcp(settings=None):
    settings = settings or load()

    @asynccontextmanager
    async def lifespan(server):
        engine = Engine(settings)
        watcher = asyncio.create_task(engine.discovery.watch())
        try:
            yield engine
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await engine.close()

    mcp = FastMCP("local-ai-router", lifespan=lifespan, instructions="For significant repository coding, pass the user's goal and constraints to orchestrate_feature. It performs cost-optimized planning, bounded file edits, tests and targeted repair. No need to restate a design first. Registered repositories and checks are required.")

    def current_engine():
        return mcp.get_context().request_context.lifespan_context

    async def with_engine(method, *args):
        async with asyncio.timeout(settings.routing.timeouts["mcp"]):
            return await getattr(current_engine(), method)(*args)

    @mcp.tool()
    async def orchestrate_feature(task: str, repo_path: str, constraints: list[str] | None = None, budget: float | None = None, execution_mode: str = "safe-auto") -> dict:
        """Autonomous cost-optimized planning and implementation. Give goal and constraints; returns files, tests, costs and risks."""
        return await with_engine("run", Request(task=task, repo_path=repo_path, constraints=constraints or [], budget=budget, execution_mode=execution_mode))

    @mcp.tool()
    async def orchestrate_bugfix(task: str, repo_path: str, constraints: list[str] | None = None, budget: float | None = None) -> dict:
        """Fix a repository bug with bounded workers and registered regression checks."""
        return await orchestrate_feature(task, repo_path, constraints, budget)

    @mcp.tool()
    async def route_task(task: str) -> dict:
        """Preview classification and eligible models without inference or edits."""
        return await with_engine("route", task)

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
    async def router_info(action: str = "status", request_id: str = "") -> dict:
        """Read status, models, costs, cache, trace or budget. Administrative tools are grouped to save prompt tokens."""
        engine = current_engine()
        if action == "models":
            return {"models": [m.model_dump() for m in settings.models]}
        if action == "trace":
            return {"trace": engine.store.traces(request_id)}
        if action in ("costs", "budget"):
            return {"costs": engine.store.costs(), "limits": settings.budgets.model_dump()}
        if action == "cache":
            return engine.store.cache_stats()
        if action == "status":
            return {"status": "ok", "mock": settings.mock, "models": len(settings.models), "registered_repositories": len(settings.repositories)}
        raise ValueError("Unknown info action")
    return mcp
