"""Native task API uses the same compiler, restrictions and execution engine as CLI/MCP."""

import json

from fastapi import Request as HTTPRequest

from . import axir, receipts
from .schema import Request


def run_status(engine, run_id):
    row = engine.store.db.execute(
        "SELECT * FROM runs WHERE id=? OR json_extract(data,'$.plan.request_id')=? ORDER BY rowid DESC LIMIT 1",
        (run_id, run_id),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown run")
    data = json.loads(row["data"])
    return {
        "run_id": row["id"],
        "request_id": data["plan"]["request_id"],
        "status": row["status"],
        "completed_tasks": list(data.get("completed", {})),
        "total_tasks": len(data["plan"]["tasks"]),
        "result": data.get("result"),
        "receipt_available": bool(
            engine.store.db.execute("SELECT 1 FROM receipts WHERE run=?", (row["id"],)).fetchone()
        ),
    }


def install(app, engine):
    from .app import bounded_json

    @app.post("/accs/v1/route")
    async def route(request: HTTPRequest):
        data = await bounded_json(request)
        if not isinstance(data.get("task"), str) or not data["task"].strip():
            raise ValueError("A task string is required")
        return await engine.route(data["task"], repo_path=data.get("repo_path"))

    @app.post("/accs/v1/plans")
    async def plan(request: HTTPRequest):
        plan = await engine.plan(Request.model_validate(await bounded_json(request)))
        return {"plan_id": plan.plan_id, "request_id": plan.request_id, "axir": axir.export(engine, plan.plan_id)}

    @app.get("/accs/v1/plans/{plan_id}")
    async def inspect(plan_id: str):
        return axir.export(engine, plan_id)

    @app.post("/accs/v1/plans/import")
    async def import_plan(request: HTTPRequest):
        data = await bounded_json(request)
        if not isinstance(data.get("repo_path"), str) or not isinstance(data.get("axir"), dict):
            raise ValueError("repo_path and axir are required")
        ir = axir.AXIR.model_validate(data["axir"])
        plan = axir.bind(engine, ir, data["repo_path"])
        return {"plan_id": plan.plan_id, "request_id": plan.request_id, "status": "planned"}

    @app.post("/accs/v1/plans/{plan_id}/execute")
    async def execute(plan_id: str, request: HTTPRequest):
        data = await bounded_json(request)
        if not isinstance(data.get("repo_path"), str):
            raise ValueError("repo_path is required")
        return await engine.execute_plan(plan_id, data["repo_path"])

    @app.post("/accs/v1/tasks")
    async def task(request: HTTPRequest):
        return await engine.run(Request.model_validate(await bounded_json(request)))

    @app.get("/accs/v1/runs/{run_id}")
    async def run(run_id: str):
        return run_status(engine, run_id)

    @app.get("/accs/v1/receipts/{run_id}")
    async def receipt(run_id: str):
        return receipts.get(engine, run_id)

    @app.get("/accs/v1/savings")
    async def savings(period: str = "today"):
        return engine.store.economics("summary", period)
