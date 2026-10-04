import asyncio, json, secrets, sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request as HTTPRequest, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from .config import load
from .engine import Engine
from .schema import Request
from .gateway import forward
from .store import BudgetExceeded
from .providers import ProviderError

def create_app(settings=None, engine=None):
    settings = settings or load()
    engine = engine or Engine(settings)
    from .savings_stream import SavingsStream
    pulse = SavingsStream(engine)
    @asynccontextmanager
    async def lifespan(app):
        from .recovery import interrupted
        interrupted(engine)
        watcher = asyncio.create_task(engine.discovery.watch())
        economics = asyncio.create_task(pulse.watch())
        try:
            async with hub_mcp.session_manager.run():
                yield
        finally:
            watcher.cancel()
            economics.cancel()
            await asyncio.gather(watcher, economics, return_exceptions=True)
            await engine.close()
    app = FastAPI(title="Accension", version="2.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.savings_stream = pulse
    from .ui import install_ui
    install_ui(app, engine)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from .safety import redact
        from pydantic import ValidationError
        message = "; ".join(".".join(str(x) for x in e["loc"]) + ": " + e["type"] for e in exc.errors()) if isinstance(exc, ValidationError) else redact(str(exc))
        return JSONResponse({"error": message}, status_code=400)

    @app.exception_handler(ProviderError)
    async def unavailable(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(BudgetExceeded)
    async def budget(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=429)

    @app.exception_handler(RuntimeError)
    async def run_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "local-ai-router", "version": "2.0.0", "mock": settings.mock}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": prefix+name, "object": "model", "created": 0, "owned_by": "local-ai-router"} for prefix in ["accension-", "router-"] for name in ["auto", "local", "cheap", "balanced", "quality", "planner"]]}

    @app.post("/v1/responses")
    async def responses(request: HTTPRequest):
        return await forward(engine, "responses", await bounded_json(request))

    @app.post("/v1/chat/completions")
    async def chat(request: HTTPRequest):
        return await forward(engine, "chat", await bounded_json(request))

    @app.post("/v1/messages")
    async def messages(request: HTTPRequest):
        return await forward(engine, "messages", await bounded_json(request))

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: HTTPRequest):
        from .providers import token_estimate
        return {"input_tokens": token_estimate(json.dumps(await bounded_json(request)))}

    @app.post("/router/run")
    async def run(request: Request):
        return await engine.run(request)

    @app.post("/router/plan")
    async def plan(request: Request):
        return (await engine.plan(request)).model_dump(mode="json")

    @app.get("/router/costs")
    async def costs():
        return engine.store.costs()

    @app.post("/router/stop")
    async def stop():
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(409, "Server lifecycle not managed here")
        server.should_exit = True
        return {"status": "stopping"}

    @app.get("/router/traces/{request_id}")
    async def trace(request_id: str):
        return engine.store.traces(request_id)

    @app.get("/router/dashboard")
    async def dashboard():
        rows = engine.store.db.execute("SELECT request, MAX(stamp) stamp FROM traces GROUP BY request ORDER BY stamp DESC LIMIT 20").fetchall()
        profiles = [dict(r) for r in engine.store.db.execute("SELECT * FROM profiles")]
        return {"costs": engine.store.costs(), "cache": engine.store.cache_stats(), "profiles": profiles,
                "recent": [dict(r) for r in rows], "models": [{"id": m.id, "enabled": m.enabled, "healthy": engine.store.healthy(m.id)} for m in settings.models]}

    from .task_api import install
    install(app, engine)
    from .mcp_server import create_mcp
    hub_mcp = create_mcp(settings, engine)
    app.router.routes.extend(hub_mcp.streamable_http_app().routes)

    return app

async def bounded_json(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 2_000_000:
            raise HTTPException(413, "Request too large")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise HTTPException(400, "JSON object required")
    return result
