"""Separate opt-in TLS app/listener, supervised by the loopback lifespan owner."""

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import socket
import ssl
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .store import WorkbenchError

LAN_PREFIX = "/workbench/api"


def route_allowed(path, method):
    relative = path.removeprefix(LAN_PREFIX)
    reads = {
        "/status",
        "/agents",
        "/activity",
        "/events",
        "/workspaces",
        "/workspaces/{entity_id}",
        "/operations/{entity_id}",
        "/snapshots/{snapshot_id}",
        "/artifacts/{entity_id}/content",
    }
    posts = {
        "/snapshots",
        "/work-items/{entity_id}/assign",
        "/work-items/{entity_id}/assignment-preview",
        "/work-items/{entity_id}/execution-preflight",
        "/work-items/{entity_id}/dependencies",
        "/{resource}/{entity_id}/commands",
    }
    if method == "GET" and relative in reads or method == "POST" and relative in posts:
        return True
    for resource in ("projects", "prds", "work-items", "comments"):
        root = "/" + resource
        if method == "GET" and relative in {root, root + "/{entity_id}", root + "/{entity_id}/revisions"}:
            return True
        if (
            method == "POST"
            and relative in {root, root + "/{entity_id}/archive"}
            or method == "PATCH"
            and relative == root + "/{entity_id}"
        ):
            return True
    return method == "GET" and relative in {
        "/work-items/by-key/{display_key}",
        "/work-items/{entity_id}/attempts",
        "/work-items/{entity_id}/evidence",
        "/work-items/{entity_id}/assignment-preview",
        "/work-items/{entity_id}/dependencies",
        "/attempts/{entity_id}",
        "/attempts/{entity_id}/receipt",
        "/attempts/{entity_id}/verification",
        "/attempts/{entity_id}/artifacts",
    }


def principal(request):
    if not getattr(request.state, "device_principal", None):
        raise WorkbenchError("UNAUTHENTICATED", "Paired device required", 401)
    return request.state.device_principal


def safe_projection(value):
    from ..observability import redact

    private = {
        "repository_ref",
        "repository",
        "repo_root",
        "root",
        "allowed_repositories",
        "allowed_workspaces",
        "allowed_projects",
        "owner_generation",
        "host_id",
        "host_instance_id",
        "process_id",
        "owner",
        "csrf_digest",
        "secret_digest",
        "source_path",
        "certificate",
        "private_key",
        "token",
    }
    if isinstance(value, dict):
        return {key: safe_projection(item) for key, item in value.items() if key not in private}
    if isinstance(value, list):
        return [safe_projection(item) for item in value]
    return redact(value)


def tls_context(config, local_port):
    if config.port == local_port:
        raise ValueError("LAN port must differ from loopback port")
    certificate, key = Path(config.certificate), Path(config.private_key)
    if not certificate.is_file() or not key.is_file():
        raise ValueError("LAN certificate and private key must be readable files")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    try:
        from datetime import datetime, timezone

        from cryptography import x509

        cert = x509.load_pem_x509_certificate(certificate.read_bytes())
        if not cert.not_valid_before_utc <= datetime.now(timezone.utc) < cert.not_valid_after_utc:
            raise ValueError("LAN certificate is not currently valid")
        try:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound as exc:
            raise ValueError("LAN certificate requires explicit DNS/IP SAN") from exc
        for origin in config.origins:
            host = urlsplit(origin).hostname
            try:
                matched = ipaddress.ip_address(host) in names.get_values_for_type(x509.IPAddress)
            except ValueError:
                matched = host in names.get_values_for_type(x509.DNSName)
            if not matched:
                raise ValueError("LAN certificate SAN must match every configured origin exactly")
    except ImportError as exc:
        raise ValueError("Install Accension's optional lan extra for certificate validation") from exc
    return context


def create_lan_app(runtime):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workbench = runtime
    config = runtime.engine.settings.workbench_lan
    hosts = {urlsplit(origin).netloc for origin in config.origins}

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.url.scheme != "https" or request.headers.get("host") not in hosts:
            return JSONResponse({"error": {"code": "INVALID_HOST", "message": "Configured HTTPS host required"}}, 403)
        origin = request.headers.get("origin")
        if origin and origin not in config.origins:
            return JSONResponse(
                {"error": {"code": "INVALID_ORIGIN", "message": "Exact configured origin required"}}, 403
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": {"code": "INVALID_ORIGIN", "message": "Same origin required"}}, 403)
        protected = request.url.path.startswith(LAN_PREFIX) or request.url.path.startswith("/session")
        if protected:
            try:
                cookie = request.cookies.get("accension_device")
                authorization = request.headers.get("authorization", "")
                if cookie and authorization:
                    raise WorkbenchError("AMBIGUOUS_AUTH", "Use one device credential", 401)
                token = cookie or (authorization[7:] if authorization.startswith("Bearer ") else None)
                actor, credential = runtime.devices.authenticate(token, browser=bool(cookie))
                if cookie and request.method not in {"GET", "HEAD"}:
                    csrf = request.headers.get("x-csrf-token", "")
                    if origin not in config.origins or not hmac.compare_digest(
                        credential["csrf_digest"], hashlib.sha256(csrf.encode()).hexdigest()
                    ):
                        raise WorkbenchError("INVALID_CSRF", "Same origin and session CSRF required", 403)
                request.state.device_principal = actor
            except WorkbenchError as exc:
                return JSONResponse(
                    {"error": {"code": exc.code, "message": "Device authentication or request validation failed."}},
                    exc.status,
                )
        response = await call_next(request)
        if protected and response.headers.get("content-type", "").startswith("application/json"):
            import json

            body = b"".join([part async for part in response.body_iterator])
            response = JSONResponse(
                safe_projection(json.loads(body)),
                response.status_code,
                headers={
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in {"content-length", "content-type"}
                },
            )
        from ..ui import CSP

        response.headers.update(
            {
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )
        return response

    @app.get("/health")
    async def health():
        return {"service": "accension-workbench-lan", "status": "ok"}

    from pydantic import Field

    from ..app import bounded_json
    from ..schema import Strict
    from .api import transport
    from .schema import UUID, Name

    class Pair(Strict):
        code: str = Field(min_length=20, max_length=128)
        name: Name

    class Claim(Strict):
        id: UUID
        challenge: str = Field(min_length=40, max_length=128)
        browser: bool = True

    @app.post("/pairing/request")
    @transport
    async def pair_request(request: Request):
        body = Pair.model_validate(await bounded_json(request))
        return runtime.devices.request(body.code, body.name, request.client.host)

    @app.post("/pairing/claim")
    @transport
    async def pair_claim(request: Request):
        body = Claim.model_validate(await bounded_json(request))
        result = runtime.devices.claim(body.id, body.challenge, body.browser, request.client.host)
        if body.browser:
            session_credential = result.pop("token")
            response = JSONResponse(result)
            # Opaque random session credential; SQLite stores its digest, never a password or service token.
            response.set_cookie(
                "accension_device",
                session_credential,
                secure=True,
                httponly=True,
                samesite="strict",
                max_age=8 * 3600,
                path="/",
            )
            return response
        return result

    @app.post("/pairing/status")
    @transport
    async def pair_status(request: Request):
        body = Claim.model_validate(await bounded_json(request))
        with runtime.devices.transaction():
            runtime.devices.rate(request.client.host)
        row = runtime.devices.pairing(body.id, body.challenge)
        return {"id": row["id"], "status": row["state"], "verification_phrase": row["phrase"]}

    from .api import install

    install(app, runtime, prefixes=(LAN_PREFIX,), principal_resolver=principal, route_filter=route_allowed)

    @app.get("/session")
    @transport
    async def session(request: Request):
        import secrets

        actor = principal(request)
        csrf = secrets.token_urlsafe(32)
        with runtime.devices.transaction():
            device, _ = runtime.devices.current(actor.device_id, actor.credential_id, actor.browser)
            if not actor.browser:
                raise WorkbenchError("FORBIDDEN", "Browser session required", 403)
            runtime.devices.store.db.execute(
                "UPDATE wb_device_sessions SET csrf_digest=? WHERE id=?",
                (hashlib.sha256(csrf.encode()).hexdigest(), actor.credential_id),
            )
        import json

        return {"csrf": csrf, "grants": json.loads(device["grants_json"])}

    @app.post("/session/logout")
    @transport
    async def logout(request: Request):
        actor = principal(request)
        with runtime.devices.transaction():
            runtime.devices.store.db.execute(
                "UPDATE wb_device_sessions SET revoked_at=? WHERE id=?", (runtime.devices.clock(), actor.credential_id)
            )
        response = JSONResponse({"status": "logged_out"})
        response.delete_cookie("accension_device", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/")
    async def shell():
        return FileResponse(Path(__file__).parents[1] / "static" / "workbench-lan.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"workbench-lan.js", "workbench.css"}:
            return JSONResponse({"error": {"code": "NOT_FOUND"}}, 404)
        return FileResponse(Path(__file__).parents[1] / "static" / name)

    @app.post("/credentials/rotate")
    @transport
    async def rotate(request: Request):
        actor, _ = runtime.devices.authenticate(request.headers.get("authorization", "")[7:])
        return runtime.devices.rotate(actor)

    return app


async def serve_pair(local_server, runtime):
    """Only local_server captures signals and owns application startup/shutdown."""
    import uvicorn

    config = runtime.engine.settings.workbench_lan
    lan_server = lan_task = sock = None
    local_task = asyncio.create_task(local_server.serve())
    try:
        if config.enabled:
            try:
                context = tls_context(config, runtime.engine.settings.port)
                while not local_server.started and not local_task.done():
                    await asyncio.sleep(0.01)
                if local_task.done():
                    return await local_task
                family = socket.AF_INET6 if ":" in config.bind else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else socket.SO_REUSEADDR,
                    1,
                )
                sock.bind((config.bind, config.port))
                sock.listen(128)
                sock.setblocking(False)
                cfg = uvicorn.Config(
                    create_lan_app(runtime),
                    host=config.bind,
                    port=config.port,
                    lifespan="off",
                    access_log=False,
                    log_level="warning",
                    proxy_headers=False,
                    timeout_graceful_shutdown=10,
                )
                cfg.load()
                cfg.ssl = context
                lan_server = uvicorn.Server(cfg)
                lan_server.capture_signals = lambda: nullcontext()

                async def run_lan():
                    try:
                        await lan_server.serve(sockets=[sock])
                    except (Exception, SystemExit) as exc:
                        runtime.lan_error = type(exc).__name__
                        logging.getLogger(__name__).warning("LAN listener stopped; loopback service remains available")

                lan_task = asyncio.create_task(run_lan())
                runtime.lan_error = None
            except (ValueError, OSError, ssl.SSLError) as exc:
                runtime.lan_error = type(exc).__name__
                logging.getLogger(__name__).warning("LAN disabled: TLS configuration or listener unavailable")
        await local_task
    finally:
        if lan_server:
            lan_server.should_exit = True
        if lan_task:
            await lan_task
        if sock:
            sock.close()
