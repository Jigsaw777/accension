"""Protocol adapters. Vendor manifests reuse these without duplicating requests."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from .errors import CapabilityUnsupported, ProviderUnavailable
from .schema import Generation, Usage


class HTTPTransport:
    async def generate(self, context, model, messages, maximum, schema=None, **kwargs):
        from .providers import usage_from

        path, payload = self.payload(model, messages, maximum, schema, context)
        from .probes import evidence, prepare, stream_probe

        probe, protocol = kwargs.get("probe"), kwargs.get("protocol")
        import secrets

        color = secrets.choice(["red", "green", "blue"]) if probe == "vision" else "red"
        path, payload = prepare(protocol, probe, path, payload, color)
        base = self.base(context)
        if probe == "streaming":
            return await stream_probe(context, base + path, payload, kwargs.get("timeout", 90))
        response = await context.client.post(
            base + path, headers=await context.headers(), json=payload, timeout=kwargs.get("timeout", 90)
        )
        response.raise_for_status()
        data = response.json()
        text = "" if probe == "embeddings" else self.text(data)
        if probe:
            data["probe_evidence"] = evidence(protocol, probe, data, text, color)
        return Generation(text=text, usage=usage_from(data), raw=data)

    def base(self, context):
        base = context.provider.endpoint.rstrip("/")
        if context.provider.kind == "ollama" and not base.endswith("/v1"):
            base += "/v1"
        if not base:
            raise ProviderUnavailable("Provider endpoint is not configured")
        return base


class OpenAIChatTransport(HTTPTransport):
    def payload(self, model, messages, maximum, schema, context):
        field = context.provider.options.get("max_tokens_parameter", "max_tokens")
        if field not in {"max_tokens", "max_completion_tokens"}:
            raise CapabilityUnsupported("Invalid token-limit parameter")
        payload = {"model": model.deployment_name, "messages": messages, field: maximum}
        if schema and model.supports_structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": False},
            }
        return "/chat/completions", payload

    def text(self, data):
        return data["choices"][0]["message"].get("content") or ""


class OpenAIResponsesTransport(HTTPTransport):
    def payload(self, model, messages, maximum, schema, context):
        payload = {"model": model.deployment_name, "input": messages, "max_output_tokens": maximum, "store": False}
        if schema and model.supports_structured_output:
            payload["text"] = {"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": False}}
        if model.supports_prompt_cache:
            from .store import cache_key

            payload["prompt_cache_key"] = cache_key(messages[0])
        return "/responses", payload

    def text(self, data):
        return "".join(
            block.get("text", "")
            for item in data.get("output", [])
            for block in item.get("content", [])
            if block.get("type") == "output_text"
        )


class AnthropicTransport(HTTPTransport):
    def payload(self, model, messages, maximum, schema, context):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        payload = {
            "model": model.deployment_name,
            "system": system,
            "messages": [m for m in messages if m["role"] != "system"],
            "max_tokens": maximum,
        }
        if model.supports_prompt_cache and system:
            payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if schema and model.supports_structured_output:
            payload["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return "/messages", payload

    def text(self, data):
        return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


class GeminiTransport(HTTPTransport):
    def base(self, context):
        if context.provider.kind != "vertex":
            return super().base(context)
        p = context.provider
        if not p.project or not p.region:
            raise ProviderUnavailable("Vertex requires a project and location")
        host = "aiplatform.googleapis.com" if p.region == "global" else p.region + "-aiplatform.googleapis.com"
        # User-supplied regions are identifiers, never URL fragments.
        import re

        if not re.fullmatch(r"[a-z0-9-]+", p.region):
            raise ProviderUnavailable("Invalid Vertex location")
        base = p.endpoint.rstrip("/") or ("https://" + host + "/v1")
        return (
            base
            + "/projects/"
            + quote(p.project, safe="")
            + "/locations/"
            + quote(p.region, safe="")
            + "/publishers/google"
        )

    def payload(self, model, messages, maximum, schema, context):
        payload = {
            "contents": [
                {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages
                if m["role"] != "system"
            ],
            "generationConfig": {"maxOutputTokens": maximum},
        }
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if schema and model.supports_structured_output:
            payload["generationConfig"].update(responseMimeType="application/json", responseJsonSchema=schema)
        return "/models/" + quote(model.deployment_name.removeprefix("models/"), safe="") + ":generateContent", payload

    def text(self, data):
        return "".join(
            part.get("text", "")
            for candidate in data.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
            if not part.get("thought")
        )


class BedrockTransport:
    async def generate(self, context, model, messages, maximum, schema=None, **kwargs):
        if kwargs.get("probe") not in {None, "text", "structured_output"}:
            raise CapabilityUnsupported("This Bedrock feature has no bounded active probe yet")
        payload = {
            "modelId": model.deployment_name,
            "messages": [
                {"role": m["role"], "content": [{"text": m["content"]}]} for m in messages if m["role"] != "system"
            ],
            "inferenceConfig": {"maxTokens": maximum},
        }
        system = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        if system:
            payload["system"] = system
        if schema and model.supports_structured_output:
            payload["outputConfig"] = {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {"jsonSchema": {"name": "result", "schema": json.dumps(schema)}},
                }
            }
        if context.provider.credential_ref or context.provider.api_key_env:
            import re

            region = context.provider.region
            if not re.fullmatch(r"[a-z0-9-]+", region):
                raise ProviderUnavailable("Bedrock requires a valid region")
            base = context.provider.endpoint.rstrip("/") or "https://bedrock-runtime." + region + ".amazonaws.com"
            payload.pop("modelId")
            response = await context.client.post(
                base + "/model/" + quote(model.deployment_name, safe="") + "/converse",
                headers=await context.headers(),
                json=payload,
                timeout=kwargs.get("timeout", 90),
            )
            response.raise_for_status()
            data = response.json()
        else:

            def invoke():
                client = context.auth.aws_client(context.provider, "bedrock-runtime")
                return client.converse(**payload)

            data = await asyncio.to_thread(invoke)
        u = data.get("usage", {})
        return Generation(
            text="".join(
                block.get("text", "") for block in data.get("output", {}).get("message", {}).get("content", [])
            ),
            usage=Usage(
                input_tokens=u.get("inputTokens", 0)
                + u.get("cacheReadInputTokens", 0)
                + u.get("cacheWriteInputTokens", 0),
                output_tokens=u.get("outputTokens", 0),
                cached_tokens=u.get("cacheReadInputTokens", 0),
                cache_write_tokens=u.get("cacheWriteInputTokens", 0),
            ),
            raw=data,
        )


class MCPTransport:
    async def generate(self, context, model, messages, maximum, schema=None, **kwargs):
        if kwargs.get("probe") not in {None, "text"}:
            raise CapabilityUnsupported("MCP inference tool does not declare a native probe contract")
        from .providers import mcp_call

        data = await mcp_call(
            context.provider,
            context.provider.tool,
            {
                "task_id": kwargs.get("task_id", "inference"),
                "instruction": messages[0]["content"],
                "context": messages[-1]["content"],
                "max_output_tokens": min(maximum, 2048),
            },
            kwargs.get("timeout", 90),
        )
        return Generation(text=data.get("text", data.get("output", json.dumps(data))))


class MockTransport:
    async def generate(self, context, model, messages, maximum, schema=None, **kwargs):
        from .mock import generate

        return generate(messages, kwargs.get("role", "executor"))


TRANSPORTS = {
    "openai_chat": OpenAIChatTransport,
    "openai_responses": OpenAIResponsesTransport,
    "anthropic_messages": AnthropicTransport,
    "google_gemini": GeminiTransport,
    "bedrock_converse": BedrockTransport,
    "mcp": MCPTransport,
    "mock": MockTransport,
}
