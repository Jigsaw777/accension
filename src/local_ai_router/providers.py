from __future__ import annotations
import asyncio, json, os, time
from collections import deque
import httpx
from .schema import Model, Generation, Usage
from .errors import (ProviderError, ProviderUnavailable, ContextOverflow, RateLimited,
                     InvalidStructuredOutput, normalize_error)
from .privacy import guard_provider, preflight

def estimate_cost(model: Model, inputs: int, outputs: int, cached: int = 0, writes: int = 0, reserve: bool = False) -> float:
    if model.input_price is None or model.output_price is None:
        raise ProviderError("Pricing is unknown; configure USD per million tokens")
    cached = min(inputs, cached)
    writes = min(inputs-cached, writes)
    write_price = model.cache_write_price if model.cache_write_price is not None else model.input_price
    if reserve:
        return (inputs*max(model.input_price, write_price, model.cached_input_price or 0)+outputs*model.output_price)/1_000_000
    return ((inputs-cached-writes)*model.input_price + outputs*model.output_price + cached*(model.cached_input_price if model.cached_input_price is not None else model.input_price) + writes*write_price)/1_000_000

def token_estimate(text: str) -> int:
    return max(1, (len(text.encode("utf-8"))+2)//3)

def token_upper_bound(value) -> int:
    # Byte-level bound is deliberately conservative for budgeting unknown tokenizers.
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + 256

def parse_json(text):
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # Deterministic syntax-only repair: trailing commas and missing terminal
        # braces. Never invent values, quotes, keys or code.
        start = min((i for i in (value.find("{"), value.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise InvalidStructuredOutput("Model did not return JSON") from None
        value = value[start:]
        stack, quoted, escaped, repaired = [], False, False, []
        for char in value:
            if quoted:
                repaired.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if not stack or stack.pop() != char:
                    raise InvalidStructuredOutput("Unbalanced model JSON") from None
                while repaired and repaired[-1].isspace():
                    repaired.pop()
                if repaired and repaired[-1] == ",":
                    repaired.pop()
            repaired.append(char)
            if not quoted and not stack:
                break
        if quoted:
            raise InvalidStructuredOutput("Truncated JSON string") from None
        try:
            return json.loads("".join(repaired) + "".join(reversed(stack)))
        except json.JSONDecodeError:
            raise InvalidStructuredOutput("Invalid model JSON") from None

def usage_from(data):
    if "usageMetadata" in data:
        u = data["usageMetadata"] or {}
        return Usage(input_tokens=u.get("promptTokenCount", 0), output_tokens=u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
                     cached_tokens=u.get("cachedContentTokenCount", 0))
    u = data.get("usage") or {}
    inputs = u.get("input_tokens", u.get("prompt_tokens", 0)) or 0
    if "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u:
        inputs += (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    return Usage(input_tokens=inputs,
                 output_tokens=u.get("output_tokens", u.get("completion_tokens", 0)) or 0,
                 cached_tokens=(u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}).get("cached_tokens", u.get("cache_read_input_tokens", 0)) or 0,
                 cache_write_tokens=u.get("cache_creation_input_tokens", 0) or 0)

async def mcp_call(provider, tool: str, args: dict, timeout: float):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {k:v for k,v in os.environ.items() if not any(x in k.lower() for x in ["api_key", "secret", "token", "password"])}
    env.update(provider.env)
    params = StdioServerParameters(command=provider.command, args=provider.args, env=env)
    async with asyncio.timeout(timeout):
        # Keep lifecycle in one task; MCP's anyio cancel scopes cannot move between tasks.
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                if result.isError:
                    raise ProviderError("MCP upstream rejected request")
                blocks = [b.text for b in result.content if getattr(b, "type", None) == "text"]
                return parse_json("\n".join(blocks))

class Providers:
    def __init__(self, settings, store, transport=None):
        self.settings, self.store = settings, store
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False,
                                       limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        self.semaphores = {m.id: asyncio.Semaphore(m.concurrency_limit) for m in settings.models}
        self.windows = {m.id: deque() for m in settings.models}
        self.token_cache = {}
        self.inference_limit = asyncio.Semaphore(settings.runtime.inference_concurrency)
        self.discovery_limit = asyncio.Semaphore(settings.runtime.discovery_concurrency)
        self.health_limit = asyncio.Semaphore(settings.runtime.health_probe_concurrency)
        self.calibration_limit = asyncio.Semaphore(settings.runtime.calibration_concurrency)
        from .auth import AuthManager
        from .provider_sdk import PluginRegistry
        self.auth = AuthManager(settings)
        self.plugins = PluginRegistry(settings.plugins.enabled)

    async def close(self):
        self.auth.close()
        await self.client.aclose()

    async def acquire_slot(self, model):
        """Streaming calls keep the global and model permit until the body closes."""
        sem = self.semaphores.setdefault(model.id, asyncio.Semaphore(model.concurrency_limit))
        await self.inference_limit.acquire()
        try:
            await sem.acquire()
        except BaseException:
            self.inference_limit.release()
            raise
        return InferencePermit(sem, self.inference_limit)

    async def headers(self, provider):
        return await self.auth.headers(provider)

    def protocol(self, provider):
        if provider.protocol:
            return provider.protocol
        manifest = self.plugins.get(provider.kind).manifest()
        legacy = {"chat": "openai_chat", "responses": "openai_responses", "messages": "anthropic_messages"}[provider.api]
        return legacy if legacy in manifest.protocols else manifest.protocols[0]

    def context(self, provider, name=None):
        from .provider_sdk import ProviderContext
        name = name or next((key for key, value in self.settings.providers.items() if value is provider), provider.kind)
        return ProviderContext(name, provider, self)

    def _rate(self, model):
        q, now = self.windows.setdefault(model.id, deque()), time.monotonic()
        while q and q[0] < now-60:
            q.popleft()
        if len(q) >= model.rate_limit:
            raise RateLimited("Configured requests-per-minute limit reached", retry_after=60 - (now-q[0]))
        q.append(now)

    async def generate(self, model, messages, role, request_id, session, budget, max_output=None, schema=None, task_id=None, probe=None, call_budget=None):
        provider = self.settings.providers[model.provider]
        guard_provider(self.settings, provider, role)
        from .config import validate_endpoint
        validate_endpoint(provider, self.settings.port)
        messages = preflight(messages, provider)
        from .roles import RoleResolver
        model = RoleResolver(self.settings, self.store).priced(model)
        if not provider.enabled or not model.enabled or model.status not in {"available", "degraded"} or not self.store.healthy(model.id):
            raise ProviderUnavailable("Provider disabled, model unavailable or circuit open")
        maximum = min(max_output or model.max_output, model.max_output)
        inputs = token_upper_bound(messages) + (token_upper_bound(schema) if schema else 0)
        if probe:
            inputs += 8192 if probe == "vision" else 1024
        if inputs + maximum > model.context_window:
            raise ContextOverflow("Context exceeds model window; retrieve fewer files, split the task or select a larger context model")
        estimate = estimate_cost(model, inputs, maximum, reserve=True)
        if call_budget is not None and estimate > call_budget + 1e-10:
            from .store import BudgetExceeded
            raise BudgetExceeded("Task call budget exceeded")
        async with self.inference_limit, self.semaphores.setdefault(model.id, asyncio.Semaphore(model.concurrency_limit)):
            self._rate(model)
            call = self.store.reserve(request_id, session, role, model, estimate, budget, task_id=task_id)
            start = time.monotonic()
            try:
                from .contracts import reserve_egress
                reserve_egress(self.store, call, request_id, model, role, inputs)
            except BaseException:
                # No bytes have been transmitted: this is a safe monetary refund.
                self.store.settle(call, 0, {}, 0)
                raise
            try:
                args = (provider, model, messages, role, maximum, schema, task_id or request_id)
                if role == "calibration":
                    async with self.calibration_limit:
                        result = await self._generate(*args, probe=probe) if probe else await self._generate(*args)
                else:
                    result = await self._generate(*args, probe=probe) if probe else await self._generate(*args)
                u = result.usage
                actual = estimate_cost(model, u.input_tokens, u.output_tokens, u.cached_tokens, u.cache_write_tokens) if (u.input_tokens or u.output_tokens) else estimate
                self.store.settle(call, actual, u.model_dump(), time.monotonic()-start)
                self.store.health_result(model.id, True)
                self.store.provider_health(model.provider, "HEALTHY")
                from .dna import observe
                observe(self.store, model.id, "reliability", True, source="transport")
                self.store.trace(request_id, "inference", role=role, model=model.id, usage=u.model_dump(), estimated_cost=actual, latency=time.monotonic()-start)
                return result
            except BaseException as exc:
                self.store.settle(call, None, {}, time.monotonic()-start, failed=True)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                error = normalize_error(exc)
                from .dna import observe
                observe(self.store, model.id, "reliability", False, source="transport")
                self.record_failure(model, error)
                raise error from None

    def record_failure(self, model, error):
        self.store.health_result(model.id, False)
        status = "AUTH_REQUIRED" if error.code == "AUTH_REQUIRED" else "RATE_LIMITED" if isinstance(error, RateLimited) else "DEGRADED"
        self.store.provider_health(model.provider, status, getattr(error, "retry_after", self.settings.routing.circuit_cooldown), error.code)

    async def _generate(self, provider, model, messages, role, maximum, schema, task_id, probe=None):
        timeout = self.settings.routing.timeouts.get(role, self.settings.routing.timeouts["local" if provider.local else "cloud"])
        protocol = {"responses_api": "openai_responses", "chat_completions": "openai_chat"}.get(probe, self.protocol(provider))
        adapter = self.plugins.get(provider.kind).create_transport(protocol)
        async with asyncio.timeout(timeout):
            return await adapter.generate(self.context(provider, model.provider), model, messages, maximum, schema, role=role, task_id=task_id, timeout=timeout, probe=probe, protocol=protocol)

    async def discover(self, name, provider):
        guard_provider(self.settings, provider, "metadata")
        if not provider.enabled:
            raise ProviderUnavailable("Provider disabled")
        async with self.discovery_limit, asyncio.timeout(self.settings.runtime.discovery_timeout):
            return await self.plugins.get(provider.kind).discover_models(self.context(provider, name))

    async def embed(self, model, texts, request_id, session, budget):
        """Optional embedding API; lexical repository retrieval has no dependency on it."""
        from .errors import CapabilityUnsupported
        if not model.supports_embeddings or not isinstance(texts, list) or not 1 <= len(texts) <= 32 or any(not isinstance(text, str) or len(text) > 100000 for text in texts):
            raise CapabilityUnsupported("Choose a verified embedding model and 1-32 bounded text inputs")
        vectors = []
        for text in texts:
            result = await self.generate(model, [{"role": "user", "content": text}], "embedding", request_id, session, budget, max_output=1, probe="embeddings")
            if not result.raw.get("probe_evidence", {}).get("embeddings"):
                raise CapabilityUnsupported("Embedding response did not contain a finite numeric vector")
            vectors.append(result.raw["data"][0]["embedding"] if "data" in result.raw else result.raw["embedding"]["values"])
        return vectors

    async def health(self, provider):
        if not provider.enabled:
            return {"status": "disabled"}
        if provider.kind == "mock":
            return {"status": "ok", "models": ["mock-worker"]}
        if provider.kind == "mcp":
            return {"status": "configured", "check": "MCP executable only"}
        try:
            guard_provider(self.settings, provider, "metadata")
            async with self.health_limit:
                return await self.plugins.get(provider.kind).health(self.context(provider))
        except Exception as exc:
            error = normalize_error(exc)
            return {"status": "auth_required" if error.code == "AUTH_REQUIRED" else "unavailable", "error": error.code}


class InferencePermit:
    def __init__(self, *semaphores):
        self.semaphores = semaphores

    def release(self):
        for semaphore in self.semaphores:
            semaphore.release()
        self.semaphores = ()
