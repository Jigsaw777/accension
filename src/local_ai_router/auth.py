"""Authentication strategies shared by provider plugins and transports."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Protocol

from .credentials import CredentialStore, read_secret
from .errors import AuthenticationRequired, normalize_error


class AuthStrategy(Protocol):
    async def headers(self, provider, manager) -> dict[str, str]: ...


class APIKeyAuth:
    async def headers(self, provider, manager):
        key = manager.vault.read(provider.credential_ref) if provider.credential_ref else read_secret(manager.settings, provider.api_key_env)
        if (provider.credential_ref or provider.api_key_env) and not key:
            raise AuthenticationRequired("Provider credential is missing; reconnect the provider")
        if not key:
            return {}
        header = provider.auth_header
        if provider.kind == "foundry" and header == "Authorization":
            header = "api-key"
        if provider.protocol == "anthropic_messages" or provider.kind in {"anthropic", "anthropic-compatible"}:
            header = "x-api-key"
        if provider.kind == "gemini":
            header = "x-goog-api-key"
        return {header: "Bearer " + key if header.lower() == "authorization" else key}


class AzureIdentityAuth:
    async def headers(self, provider, manager):
        from .azure_auth import get_token
        key = await asyncio.to_thread(get_token, manager.settings, "https://cognitiveservices.azure.com/.default")
        return {"Authorization": "Bearer " + key}


class CommandAuth:
    async def headers(self, provider, manager):
        if not provider.token_command:
            raise AuthenticationRequired("Credential command is not configured")
        cache_id = json.dumps(provider.token_command)
        cached = manager.cache.get(cache_id)
        if cached and cached[1] > time.time():
            return {"Authorization": "Bearer " + cached[0]}
        proc = await asyncio.create_subprocess_exec(*provider.token_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode or len(out) > 16384:
            raise AuthenticationRequired("Credential command failed; sign in with the provider CLI")
        key = out.decode().strip()
        if not key or "\n" in key:
            raise AuthenticationRequired("Credential command returned an invalid token")
        manager.cache[cache_id] = (key, time.time() + 240)
        return {"Authorization": "Bearer " + key}


class GoogleADCAuth:
    async def headers(self, provider, manager):
        def token():
            cache_id = (provider.project, provider.service_account_file)
            credentials = manager.google_credentials.get(cache_id)
            if credentials is None:
                try:
                    import google.auth
                    if provider.service_account_file:
                        from google.oauth2 import service_account
                        credentials = service_account.Credentials.from_service_account_file(provider.service_account_file, scopes=["https://www.googleapis.com/auth/cloud-platform"])
                    else:
                        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                except ImportError:
                    raise AuthenticationRequired("Install accension[google] to use Google credentials") from None
                manager.google_credentials[cache_id] = credentials
            if not credentials.valid:
                from google.auth.transport.requests import Request
                import requests
                with requests.Session() as session:
                    session.trust_env = False
                    credentials.refresh(Request(session=session))
            return credentials.token
        return {"Authorization": "Bearer " + await asyncio.to_thread(token)}


class NoAuth:
    async def headers(self, provider, manager):
        return {}


class AuthManager:
    def __init__(self, settings):
        self.settings, self.vault = settings, CredentialStore(settings)
        self.cache, self.google_credentials, self.clients = {}, {}, {}
        self.strategies = {"api_key": APIKeyAuth(), "azure_identity": AzureIdentityAuth(), "command": CommandAuth(), "google_adc": GoogleADCAuth(), "none": NoAuth(), "aws_chain": NoAuth()}

    async def headers(self, provider):
        from .privacy import guard_provider
        guard_provider(self.settings, provider, "metadata")
        mode = provider.auth
        if mode == "auto":
            mode = "command" if provider.token_command else "azure_identity" if provider.azure_identity else "google_adc" if provider.kind == "vertex" else "api_key"
        try:
            headers = await self.strategies[mode].headers(provider, self)
        except Exception as exc:
            raise normalize_error(exc) from None
        headers.update({"Content-Type": "application/json", "X-Local-Router-Hop": "1"})
        if provider.protocol == "anthropic_messages" or provider.api == "messages" or provider.kind in {"anthropic", "anthropic-compatible"}:
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def aws_client(self, provider, service):
        from .privacy import guard_provider
        guard_provider(self.settings, provider, "metadata")
        key = (provider.region, provider.profile, service)
        if key not in self.clients:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise AuthenticationRequired("Install accension[aws] to use the AWS credential chain") from None
            session = boto3.Session(profile_name=provider.profile or None, region_name=provider.region or None)
            # Each paid attempt needs its own reservation. SDK retries are disabled.
            self.clients[key] = session.client(service, config=Config(connect_timeout=5, read_timeout=self.settings.routing.timeouts["cloud"],
                retries={"total_max_attempts": 1, "mode": "standard"}, proxies={}, max_pool_connections=10))
        return self.clients[key]

    def close(self):
        for client in self.clients.values():
            client.close()
