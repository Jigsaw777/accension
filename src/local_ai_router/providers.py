from __future__ import annotations
import asyncio, json, os, time
from collections import deque
import httpx
from .schema import Model, Generation, Usage

class ProviderError(RuntimeError):
    pass

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
    return json.loads(value)

def usage_from(data):
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

    async def close(self):
        await self.client.aclose()

    async def headers(self, provider):
        headers = {"Content-Type": "application/json", "X-Local-Router-Hop": "1"}
        from .credentials import read_secret
        key = read_secret(self.settings, provider.api_key_env)
        if provider.azure_identity:
            from .azure_auth import get_token
            key = await asyncio.to_thread(get_token, self.settings, "https://cognitiveservices.azure.com/.default")
        if provider.token_command:
            cached = self.token_cache.get(provider.endpoint)
            if cached and cached[1] > time.time():
                key = cached[0]
            else:
                proc = await asyncio.create_subprocess_exec(*provider.token_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), 15)
                except BaseException:
                    proc.kill(); await proc.wait(); raise
                if proc.returncode:
                    raise ProviderError("Azure token command failed; authenticate with Azure CLI")
                key = out.decode().strip()
                self.token_cache[provider.endpoint] = (key, time.time()+240)
        if provider.api_key_env and not key:
            raise ProviderError("Missing provider credential environment variable")
        if key:
            if provider.kind == "anthropic":
                headers["x-api-key"] = key
            else:
                headers[provider.auth_header] = "Bearer " + key if provider.auth_header.lower() == "authorization" else key
        if provider.kind == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    def _rate(self, model):
        q, now = self.windows[model.id], time.monotonic()
        while q and q[0] < now-60:
            q.popleft()
        if len(q) >= model.rate_limit:
            raise ProviderError("Configured requests-per-minute limit reached")
        q.append(now)

    async def generate(self, model, messages, role, request_id, session, budget, max_output=None, schema=None, task_id=None):
        provider = self.settings.providers[model.provider]
        if not provider.enabled or not model.enabled or not self.store.healthy(model.id):
            raise ProviderError("Provider disabled or circuit open")
        maximum = min(max_output or model.max_output, model.max_output)
        inputs = token_upper_bound(messages)
        if inputs + maximum > model.context_window:
            raise ProviderError("Context exceeds model window (conservative token bound)")
        estimate = estimate_cost(model, inputs, maximum, reserve=True)
        async with self.semaphores[model.id]:
            self._rate(model)
            call = self.store.reserve(request_id, session, role, model, estimate, budget)
            start = time.monotonic()
            try:
                result = await self._generate(provider, model, messages, role, maximum, schema, task_id or request_id)
                u = result.usage
                actual = estimate_cost(model, u.input_tokens, u.output_tokens, u.cached_tokens, u.cache_write_tokens) if (u.input_tokens or u.output_tokens) else estimate
                self.store.settle(call, actual, u.model_dump(), time.monotonic()-start)
                self.store.health_result(model.id, True)
                self.store.trace(request_id, "inference", role=role, model=model.id, usage=u.model_dump(), estimated_cost=actual, latency=time.monotonic()-start)
                return result
            except BaseException as exc:
                self.store.settle(call, None, {}, time.monotonic()-start, failed=True)
                self.store.health_result(model.id, False)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                # Never include upstream body, URL or request headers in errors.
                raise ProviderError(f"Provider call failed: {type(exc).__name__}") from None

    async def _generate(self, provider, model, messages, role, maximum, schema, task_id):
        timeout = self.settings.routing.timeouts.get(role, self.settings.routing.timeouts["local" if provider.local else "cloud"])
        if provider.kind == "mock":
            from .mock import generate
            return generate(messages, role)
        if provider.kind == "mcp":
            data = await mcp_call(provider, provider.tool, {"task_id": task_id, "instruction": messages[0]["content"], "context": messages[-1]["content"], "max_output_tokens": min(maximum, 2048)}, timeout)
            return Generation(text=data.get("text", data.get("output", json.dumps(data))))
        headers = await self.headers(provider)
        if provider.kind == "anthropic":
            payload = {"model": model.deployment_name, "system": "\n".join(m["content"] for m in messages if m["role"] == "system"),
                       "messages": [m for m in messages if m["role"] != "system"], "max_tokens": maximum}
            path = "/messages"
            if model.supports_prompt_cache and payload["system"]:
                payload["system"] = [{"type": "text", "text": payload["system"], "cache_control": {"type": "ephemeral"}}]
        elif provider.api == "responses":
            payload = {"model": model.deployment_name, "input": messages, "max_output_tokens": maximum, "store": False}
            path = "/responses"
            if schema and model.supports_structured_output:
                payload["text"] = {"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": False}}
            if model.supports_prompt_cache:
                from .store import cache_key
                payload["prompt_cache_key"] = cache_key(role, messages[0])
        else:
            path = "/chat/completions"
            payload = {"model": model.deployment_name, "messages": messages, "max_tokens": maximum}
            if schema and model.supports_structured_output:
                payload["response_format"] = {"type": "json_object"}
        response = await self.client.post(provider.endpoint.rstrip("/")+path, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if provider.kind == "anthropic":
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        elif provider.api == "responses":
            text = "".join(b.get("text", "") for item in data.get("output", []) for b in item.get("content", []) if b.get("type") == "output_text")
        else:
            text = data["choices"][0]["message"].get("content") or ""
        return Generation(text=text, usage=usage_from(data), raw=data)

    async def health(self, provider):
        if not provider.enabled:
            return {"status": "disabled"}
        if provider.kind == "mock":
            return {"status": "ok", "models": ["mock-worker"]}
        if provider.kind == "mcp":
            return {"status": "configured", "check": "MCP executable only"}
        try:
            r = await self.client.get(provider.endpoint.rstrip("/")+"/models", headers=await self.headers(provider), timeout=3)
            r.raise_for_status()
            return {"status": "ok", "models": [x.get("id") for x in r.json().get("data", [])]}
        except Exception as exc:
            return {"status": "unavailable", "error": type(exc).__name__}
