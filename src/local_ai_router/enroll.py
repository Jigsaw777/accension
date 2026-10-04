"""One-use localhost enrollment form. Start only for explicit credential setup."""

import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .credentials import save_secret


def create_enrollment(settings):
    token = secrets.token_urlsafe(32)
    (settings.state / "enrollment-token").write_text(token)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    used = False

    @app.get("/{nonce}")
    async def page(nonce: str):
        if not secrets.compare_digest(nonce, token) or used:
            raise HTTPException(404)
        return HTMLResponse(
            """<!doctype html><title>Router credential setup</title><h1>Foundry credential setup</h1>
        <p>Save existing API key encrypted with Windows DPAPI for this OS user.</p>
        <form method="post"><label>Foundry API key <input name="key" type="password" autocomplete="off" required></label><button>Save encrypted credential</button></form>""",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/{nonce}")
    async def save(nonce: str, request: Request):
        nonlocal used
        if not secrets.compare_digest(nonce, token) or used:
            raise HTTPException(404)
        if request.headers.get("origin") not in (None, "http://127.0.0.1:8766"):
            raise HTTPException(403)
        form = await request.form()
        save_secret(settings, "AZURE_OPENAI_API_KEY", str(form.get("key", "")))
        used = True
        (settings.state / "enrollment-token").unlink(missing_ok=True)
        return HTMLResponse("<h1>Credential saved</h1><p>Encrypted for your Windows account. No key was logged.</p>")

    return app
