"""Loopback management UI: same-origin sessions, CSRF, no browser secret storage."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .management import Management

STATIC = Path(__file__).parent / "static"
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


def install_ui(app, engine):
    from .app import bounded_json

    manager = Management(engine)
    app.state.management = manager
    sessions = {}

    @app.middleware("http")
    async def guard(request, call_next):
        try:
            host = urlsplit("http://" + request.headers.get("host", ""))
            valid_host = (
                host.hostname in {"localhost", "127.0.0.1", "::1"}
                and host.username is None
                and host.password is None
                and not host.path
            )
            _ = host.port  # Parse now so malformed port numbers fail the Host check.
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse({"error": "Loopback Host required"}, 403)
        if request.headers.get("x-local-router-hop"):
            return JSONResponse({"error": "Recursive router call refused"}, 508)
        origin = request.headers.get("origin")
        expected = request.url.scheme + "://" + request.headers.get("host", "")
        if origin and origin != expected:
            return JSONResponse({"error": "Cross-origin browser request refused"}, 403)
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return JSONResponse({"error": "Same-origin browser access required"}, 403)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": "Invalid content length"}, 400)
        if not 0 <= length <= 2_000_000:
            return JSONResponse({"error": "Request too large"}, 413)
        now = time.monotonic()
        for key, value in list(sessions.items()):
            if value["expires"] < now:
                sessions.pop(key, None)
        session = sessions.get(request.cookies.get("accension_session", ""))
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ") or request.headers.get(
            "x-api-key", ""
        )
        authenticated = bool(supplied) and secrets.compare_digest(supplied, engine.settings.token)
        document = request.url.path in {"/", "/ui", "/ui/"} and request.method in {"GET", "HEAD"}
        public = document or request.url.path in {"/health", "/static/app.js", "/static/style.css"}
        if not public and not authenticated:
            if not session or not request.url.path.startswith("/ui/"):
                return JSONResponse(
                    {"error": "Open /ui to establish a local browser session; API clients require a service token"}, 401
                )
            if request.method not in {"GET", "HEAD"} and (
                origin != expected
                or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"])
            ):
                return JSONResponse({"error": "Same-origin CSRF token required"}, 403)
        request.state.ui_session = session
        response = await call_next(request)
        if document and not session:
            if len(sessions) >= 128:
                sessions.pop(next(iter(sessions)))
            sid = secrets.token_urlsafe(32)
            sessions[sid] = {"csrf": secrets.token_urlsafe(32), "expires": now + 8 * 3600}
            response.set_cookie(
                "accension_session",
                sid,
                httponly=True,
                samesite="strict",
                max_age=8 * 3600,
                path="/ui",
                secure=request.url.scheme == "https",
            )
        response.headers.update(
            {
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )
        return response

    @app.get("/")
    @app.get("/ui")
    @app.get("/ui/")
    async def home():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name, media_type="text/javascript" if name.endswith("js") else "text/css")

    @app.get("/ui/session")
    async def session(request: Request):
        return {"csrf": request.state.ui_session["csrf"] if request.state.ui_session else None}

    @app.get("/ui/state")
    async def state():
        from . import __version__
        from .policy import PRESETS
        from .recovery import list_runs

        page = engine.store.models_page()
        from .schema import Model

        return {
            "version": __version__,
            "mock": engine.settings.mock,
            "providers": manager.providers(),
            "manifests": manager.manifests(),
            "models": [Model.model_validate(m).profile() for m in page["items"]],
            "models_total": page["total"],
            "role_policies": {k: v.model_dump() for k, v in engine.settings.roles.items()},
            "roles": {k: compact_role(v) for k, v in engine.router.roles.all().items()},
            "policy": manager.export_profile(),
            "presets": list(PRESETS),
            "repositories": [r.model_dump() for r in engine.settings.repositories],
            "dashboard": manager.dashboard(),
            "runs": list_runs(engine),
            "integrations": {
                "endpoint": f"http://127.0.0.1:{engine.settings.port}/v1",
                "mcp_command": "accs mcp",
                "virtual_models": [
                    "accension-auto",
                    "accension-local",
                    "accension-cheap",
                    "accension-balanced",
                    "accension-quality",
                    "accension-planner",
                ],
            },
        }

    @app.get("/ui/models")
    async def models(provider: str | None = None, search: str | None = None, offset: int = 0, limit: int = 50):
        return engine.store.models_page(provider, search, offset, limit)

    @app.get("/ui/skills")
    async def skills(search: str = "", offset: int = 0, limit: int = 50):
        return (
            {"items": engine.skills.search(search, limit=limit, offset=offset)}
            if search
            else engine.skills.list(offset, limit)
        )

    @app.get("/ui/presets")
    async def skill_presets(offset: int = 0, limit: int = 50):
        return engine.skills.presets(offset, limit)

    @app.get("/ui/logs")
    async def logs(
        level: str | None = None,
        component: str | None = None,
        run: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ):
        from .observability import read_logs

        return {
            "items": read_logs(engine.settings, level=level, component=component, run=run, since=since, limit=limit)
        }

    @app.get("/ui/savings")
    async def savings(period: str | None = None):
        return engine.store.economics("state", period) or {"error": "Savings temporarily unavailable"}

    @app.get("/ui/savings/events")
    async def savings_events(request: Request):
        return StreamingResponse(
            app.state.savings_stream.events(request),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.get("/ui/models/dna")
    async def dna(model: str):
        from .dna import profile

        selected = next((m for m in engine.settings.models if m.id == model), None)
        if selected is None:
            raise HTTPException(404, "Unknown model")
        return profile(engine.store, selected)

    @app.get("/ui/receipts/{run_id}")
    async def receipt(run_id: str):
        from .receipts import get

        return get(engine, run_id)

    @app.post("/ui/action/{name}")
    async def action(name: str, request: Request):
        try:
            return await manager.action(name, await bounded_json(request))
        except (KeyError, StopIteration):
            raise HTTPException(400, "Missing or unknown configuration item") from None

    @app.get("/ui/traces/{request_id}")
    async def traces(request_id: str):
        return engine.store.traces(request_id)

    @app.get("/ui/config/export")
    async def export():
        return manager.export_profile()

    return manager


def compact_role(value, limit=20):
    graph = value.get("fallback_graph", {})
    return {
        **value,
        "candidate_count": len(value.get("candidates", [])),
        "candidates": value.get("candidates", [])[:limit],
        "fallback_graph": {
            "nodes": graph.get("nodes", [])[:limit],
            "edges": graph.get("edges", [])[: max(0, limit - 1)],
        },
    }
