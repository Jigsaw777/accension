"""Authenticated loopback daemon client; never starts an implicit worker."""

import json
from urllib.parse import quote

import httpx

from .store import WorkbenchError


class WorkbenchClient:
    def __init__(self, settings, *, transport=None):
        host = "[::1]" if settings.host == "::1" else settings.host
        if settings.host not in {"localhost", "127.0.0.1", "::1"}:
            raise WorkbenchError("INVALID_DAEMON", "Workbench daemon must be loopback", 422)
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=f"http://{host}:{settings.port}",
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=3),
            headers={"Authorization": "Bearer " + settings.token},
            transport=transport,
        )
        self.verified = False

    async def close(self):
        await self.http.aclose()

    async def _request(self, method, path, **kwargs):
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise WorkbenchError("RESPONSE_TOO_LARGE", "Daemon response exceeds limit", 502)
                if 300 <= response.status_code < 400:
                    raise WorkbenchError("INVALID_DAEMON", "Daemon redirects are refused", 502)
                try:
                    body = (
                        raw.decode("utf-8")
                        if response.headers.get("content-type", "").startswith("application/yaml")
                        else json.loads(raw)
                    )
                except ValueError as exc:
                    raise WorkbenchError("INVALID_DAEMON", "Daemon returned invalid JSON", 502) from exc
                if response.status_code >= 400:
                    error = body.get("error", {}) if isinstance(body, dict) else {}
                    raise WorkbenchError(
                        error.get("code", "DAEMON_ERROR") if isinstance(error, dict) else "DAEMON_ERROR",
                        error.get("message", "Daemon rejected request")
                        if isinstance(error, dict)
                        else "Daemon rejected request",
                        response.status_code,
                    )
                return body
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise WorkbenchError(
                "DAEMON_UNAVAILABLE",
                "Workbench daemon is stopped. Run accs start, then retry with the same command ID.",
                503,
            ) from exc
        except httpx.TimeoutException as exc:
            raise WorkbenchError(
                "COMMAND_UNCERTAIN", "Daemon response timed out. Retry with the same command ID to reconcile.", 503
            ) from exc

    async def request(self, method, suffix, *, params=None, body=None, human_action=False):
        if not self.verified:
            health = await self._request("GET", "/health")
            if (
                not isinstance(health, dict)
                or health.get("service") != "local-ai-router"
                or health.get("status") != "ok"
            ):
                raise WorkbenchError("INVALID_DAEMON", "Loopback service is not Accension", 502)
            self.verified = True
        if not suffix.startswith("/") or ".." in suffix or "?" in suffix or "#" in suffix:
            raise WorkbenchError("INVALID_PATH", "Invalid Workbench resource path", 422)
        return await self._request(
            method,
            "/accs/v1/workbench" + suffix,
            params=params,
            json=body,
            headers={"X-Workbench-Operator-Action": "human"} if human_action else {},
        )

    async def item_id(self, value, workspace_id):
        from pydantic import TypeAdapter

        from .schema import UUID

        try:
            return TypeAdapter(UUID).validate_python(value)
        except ValueError:
            item = await self.request(
                "GET", "/work-items/by-key/" + quote(value, safe=""), params={"workspace_id": workspace_id}
            )
            return item["id"]
