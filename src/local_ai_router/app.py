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
    @asynccontextmanager
    async def lifespan(app):
        watcher = asyncio.create_task(engine.discovery.watch())
        try:
            yield
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await engine.close()
    app = FastAPI(title="Local AI Router", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    token = settings.token

    @app.middleware("http")
    async def guard(request, call_next):
        if request.headers.get("x-local-router-hop"):
            return JSONResponse({"error": "Recursive router call refused"}, status_code=508)
        if request.headers.get("origin"):
            return JSONResponse({"error": "Cross-origin browser requests disabled"}, status_code=403)
        if request.url.path not in ("/health", "/"):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ") or request.headers.get("x-api-key", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": "Invalid content length"}, status_code=400)
        if length < 0 or length > 2_000_000:
            return JSONResponse({"error": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

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
        return {"status": "ok", "service": "local-ai-router", "version": "0.1.0", "mock": settings.mock}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "local-ai-router"} for m in ["router-auto", "router-local", "router-cheap", "router-balanced", "router-quality", "router-planner"]]}

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

    @app.get("/")
    async def home():
        return HTMLResponse('''<!doctype html><meta charset="utf-8"><title>Local AI Router</title>
        <style>body{font:16px system-ui;max-width:950px;margin:40px auto;background:#111827;color:#e5e7eb}input,button{padding:10px}pre{white-space:pre-wrap}h1{color:#6ee7b7}</style>
        <h1>Local AI Router</h1><p>Loopback gateway. Enter local API token to inspect costs and traces.</p>
        <input id="token" type="password" placeholder="Local API token"><button id="refresh">Refresh</button><pre id="result"></pre>
        <script>document.getElementById('refresh').onclick=async()=>{const r=await fetch('/router/dashboard',{headers:{Authorization:'Bearer '+document.getElementById('token').value}});document.getElementById('result').textContent=JSON.stringify(await r.json(),null,2)}</script>''')
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
