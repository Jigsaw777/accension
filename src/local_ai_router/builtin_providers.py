"""Built-in manifests compose inventory strategies with reusable transports."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from .errors import AuthenticationRequired, ProviderUnavailable
from .provider_sdk import ConfigField, InventoryResult, ProviderManifest, ProviderPlugin


async def get_json(context, path, params=None, headers=None):
    response = await context.client.get(
        context.provider.endpoint.rstrip("/") + path,
        headers=await context.headers() if headers is None else headers,
        params=params,
        timeout=context.settings.discovery.probe_timeout if context.provider.local else 10,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ProviderUnavailable("Invalid provider inventory")
    return data


class HTTPInventory:
    async def discover(self, context):
        models = []
        params = {}
        cursors = set()
        while True:
            data = await get_json(context, "/models", params)
            if not isinstance(data.get("data"), list):
                raise ProviderUnavailable("Incomplete model inventory")
            for item in data["data"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ProviderUnavailable("Invalid model inventory item")
                capabilities = {
                    k: v
                    for k, v in item.get("capabilities", {}).items()
                    if isinstance(v, bool)
                    and k
                    in {"text", "code", "tools", "vision", "structured_output", "reasoning", "streaming", "embeddings"}
                }
                extra = {
                    "capabilities": capabilities,
                    "capabilities_source": "provider_metadata" if capabilities else "conservative_unknown",
                }
                if isinstance(item.get("context_length"), int) and item["context_length"] > 0:
                    extra["context_window"] = item["context_length"]
                models.append(context.descriptor(item["id"], **extra))
            if not data.get("has_more"):
                return InventoryResult(models=models)
            last = data.get("last_id") or (data["data"][-1].get("id") if data["data"] else None)
            if not last or last in cursors:
                raise ProviderUnavailable("Inventory pagination did not progress")
            params["after_id"] = last
            cursors.add(last)


class OpenRouterInventory(HTTPInventory):
    async def discover(self, context):
        data = await get_json(context, "/models")
        if not isinstance(data.get("data"), list):
            raise ProviderUnavailable("Incomplete model inventory")
        models = []
        for item in data["data"]:
            remote_id = item["id"]
            supported = item.get("supported_parameters", [])
            modalities = item.get("architecture", {}).get("input_modalities", [])
            outputs = item.get("architecture", {}).get("output_modalities", [])
            fields = {
                "capabilities_source": "provider_metadata",
                "supports_tools": "tools" in supported,
                "supports_structured_output": "response_format" in supported or "structured_outputs" in supported,
                "supports_vision": "image" in modalities,
                "supports_text": "text" in outputs,
                "supports_chat_completions": "text" in outputs,
                "context_window": item.get("context_length") or 32768,
                "max_output": (item.get("top_provider") or {}).get("max_completion_tokens") or 4096,
            }
            pricing = item.get("pricing") or {}
            # Additional request/image/tool charges are not representable as token-only prices.
            extra_cost = any(
                float(pricing.get(key) or 0) != 0
                for key in ("request", "image", "web_search", "internal_reasoning", "audio", "input_audio_cache")
            )
            if not extra_cost and pricing.get("prompt") is not None and pricing.get("completion") is not None:
                fields.update(
                    input_price=float(pricing["prompt"]) * 1_000_000,
                    output_price=float(pricing["completion"]) * 1_000_000,
                    pricing_status="provider_reported",
                    pricing_source="openrouter_models",
                    pricing_updated_at=time.time(),
                )
                if pricing.get("input_cache_read") is not None:
                    fields["cached_input_price"] = float(pricing["input_cache_read"]) * 1_000_000
            models.append(context.descriptor(remote_id, **fields))
        return InventoryResult(
            models=models,
            source="authenticated_provider_catalog",
            warnings=[
                "Catalog presence does not guarantee account access; generation may require provider activation."
            ],
        )


class OllamaInventory:
    async def discover(self, context):
        # Both /v1 and native root URLs are accepted for compatibility.
        base = context.provider.endpoint.rstrip("/").removesuffix("/v1")
        response = await context.client.get(base + "/api/tags", timeout=context.settings.discovery.probe_timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get("models"), list):
            raise ProviderUnavailable("Invalid Ollama inventory")
        loaded = None
        try:
            runtime = await context.client.get(base + "/api/ps", timeout=context.settings.discovery.probe_timeout)
            runtime.raise_for_status()
            loaded = {item.get("name"): item for item in runtime.json().get("models", [])}
        except Exception:
            pass  # Runtime status is advisory; inventory remains useful offline.
        models = []
        for item in data["models"]:
            name = item.get("name") or item.get("model")
            if not name:
                raise ProviderUnavailable("Invalid Ollama model ID")
            active = (loaded or {}).get(name, {})
            fields = {
                "loaded": name in loaded if loaded is not None else None,
                "ram_estimate_mb": item.get("size", 0) / 1024**2 or None,
                "vram_estimate_mb": active.get("size_vram", 0) / 1024**2 or None,
            }
            models.append(context.descriptor(name, **fields))
        return InventoryResult(models=models, source="local_runtime")


class GeminiInventory:
    async def discover(self, context):
        models, params = [], {}
        cursors = set()
        while True:
            data = await get_json(context, "/models", params)
            if not isinstance(data.get("models"), list):
                raise ProviderUnavailable("Invalid Gemini inventory")
            for item in data["models"]:
                methods = item.get("supportedGenerationMethods", [])
                models.append(
                    context.descriptor(
                        item["name"].removeprefix("models/"),
                        supports_text="generateContent" in methods,
                        supports_embeddings="embedContent" in methods,
                        context_window=item.get("inputTokenLimit") or 32768,
                        max_output=item.get("outputTokenLimit") or 4096,
                        capabilities_source="provider_metadata",
                    )
                )
            token = data.get("nextPageToken")
            if not token:
                return InventoryResult(models=models)
            if token in cursors:
                raise ProviderUnavailable("Inventory pagination did not progress")
            params["pageToken"] = token
            cursors.add(token)


class BedrockInventory:
    async def discover(self, context):
        if context.provider.credential_ref or context.provider.api_key_env:
            # Runtime API keys don't grant control-plane inventory permissions.
            return InventoryResult(
                models=[
                    context.descriptor(m, discovered_from="explicit_configuration") for m in context.provider.model_ids
                ],
                complete=False,
                warnings=["Runtime API credential: configure model IDs; inventory requires AWS identity credentials."],
            )

        def collect():
            client = context.auth.aws_client(context.provider, "bedrock")
            data = client.list_foundation_models()
            records = []
            for item in data.get("modelSummaries", []):
                if "ON_DEMAND" not in item.get("inferenceTypesSupported", []):
                    continue
                records.append(
                    context.descriptor(
                        item["modelId"],
                        status="deprecated"
                        if item.get("modelLifecycle", {}).get("status") == "LEGACY"
                        else "available",
                        supports_text="TEXT" in item.get("outputModalities", []),
                        supports_vision="IMAGE" in item.get("inputModalities", []),
                        supports_streaming=bool(item.get("responseStreamingSupported")),
                        capabilities_source="provider_metadata",
                    )
                )
            # Region/account profiles are deployment evidence distinct from public names.
            if client.can_paginate("list_inference_profiles"):
                for page in client.get_paginator("list_inference_profiles").paginate():
                    for item in page.get("inferenceProfileSummaries", []):
                        records.append(
                            context.descriptor(
                                item["inferenceProfileId"],
                                status="available" if item.get("status") == "ACTIVE" else "unavailable",
                                discovered_from="bedrock_inference_profile",
                            )
                        )
            return records

        return InventoryResult(
            models=await asyncio.to_thread(collect),
            source="regional_provider_inventory",
            warnings=[
                "Foundation model inventory does not prove account entitlement or Converse compatibility; optional probes verify access."
            ],
        )


class ConfiguredInventory:
    async def discover(self, context):
        # Vertex publisher catalogs are not deployment/access evidence. Preserve
        # exact user-selected IDs; do not invent model names from a public catalog.
        await context.headers()
        return InventoryResult(
            models=[
                context.descriptor(m, discovered_from="explicit_configuration") for m in context.provider.model_ids
            ],
            complete=False,
            source="explicit_configuration",
            warnings=["Configure exact model IDs or deployments; active probes verify access."],
        )


class FoundryInventory(ConfiguredInventory):
    async def discover(self, context):
        p, d = context.provider, context.settings.discovery
        subscription = p.options.get("subscription") or d.azure_subscription
        group = p.options.get("resource_group") or d.azure_resource_group
        account = p.options.get("account") or d.azure_account
        if not (subscription and group and account):
            return await super().discover(context)
        import os

        token = os.getenv(d.management_token_env, "")
        if not token and d.token_command:
            from .auth import CommandAuth

            temporary = p.model_copy(update={"token_command": d.token_command})
            token = (await CommandAuth().headers(temporary, context.auth))["Authorization"].removeprefix("Bearer ")
        if not token:
            from .azure_auth import get_token

            token = await asyncio.to_thread(get_token, context.settings, "https://management.azure.com/.default")
        if not token:
            raise AuthenticationRequired("Azure management identity required for deployment discovery")
        base = (
            "https://management.azure.com/subscriptions/"
            + quote(str(subscription), safe="")
            + "/resourceGroups/"
            + quote(str(group), safe="")
            + "/providers/Microsoft.CognitiveServices/accounts/"
            + quote(str(account), safe="")
            + "/deployments"
        )
        url, models = base + "?api-version=2024-10-01", []
        cursors = set()
        while True:
            r = await context.client.get(url, headers={"Authorization": "Bearer " + token}, timeout=10)
            r.raise_for_status()
            data = r.json()
            for item in data.get("value", []):
                properties = item.get("properties", {})
                if properties.get("provisioningState") != "Succeeded":
                    continue
                mapping = {
                    "chatCompletion": "chat_completions",
                    "responses": "responses_api",
                    "toolCalling": "tools",
                    "jsonObjectResponse": "structured_output",
                }
                caps = {
                    mapping[k]: str(v).lower() == "true"
                    for k, v in properties.get("capabilities", {}).items()
                    if k in mapping
                }
                models.append(
                    context.descriptor(
                        item["name"],
                        capabilities=caps,
                        capabilities_source="provider_metadata",
                        discovered_from="azure_deployments",
                    )
                )
            url = data.get("nextLink")
            if not url:
                return InventoryResult(models=models, source="azure_deployments")
            if not url.startswith(base + "?"):
                raise ProviderUnavailable("Unexpected deployment pagination endpoint")
            if url in cursors:
                raise ProviderUnavailable("Inventory pagination did not progress")
            cursors.add(url)


class BuiltinProvider(ProviderPlugin):
    def __init__(self, manifest, inventory):
        self._manifest, self.inventory_strategy = manifest, inventory

    def manifest(self):
        return self._manifest

    async def discover_models(self, context):
        return await self.inventory_strategy.discover(context)


def builtins():
    http, configured = HTTPInventory(), ConfiguredInventory()
    entries = [
        ("openai", "OpenAI", "https://api.openai.com/v1", ["openai_chat", "openai_responses"], http, False),
        ("anthropic", "Anthropic", "https://api.anthropic.com/v1", ["anthropic_messages"], http, False),
        (
            "foundry",
            "Microsoft Foundry / Azure OpenAI",
            "",
            ["openai_responses", "openai_chat", "anthropic_messages"],
            FoundryInventory(),
            False,
        ),
        ("bedrock", "AWS Bedrock", "", ["bedrock_converse"], BedrockInventory(), False),
        ("vertex", "Google Vertex AI", "", ["google_gemini"], configured, False),
        (
            "gemini",
            "Google Gemini API",
            "https://generativelanguage.googleapis.com/v1beta",
            ["google_gemini"],
            GeminiInventory(),
            False,
        ),
        ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", ["openai_chat"], OpenRouterInventory(), False),
        ("groq", "Groq", "https://api.groq.com/openai/v1", ["openai_chat"], http, False),
        ("together", "Together AI", "https://api.together.xyz/v1", ["openai_chat"], http, False),
        ("fireworks", "Fireworks AI", "https://api.fireworks.ai/inference/v1", ["openai_chat"], http, False),
        ("mistral", "Mistral", "https://api.mistral.ai/v1", ["openai_chat"], http, False),
        ("cerebras", "Cerebras", "https://api.cerebras.ai/v1", ["openai_chat"], http, False),
        ("huggingface", "Hugging Face Inference", "https://router.huggingface.co/v1", ["openai_chat"], http, False),
        ("openai-compatible", "OpenAI-compatible endpoint", "", ["openai_chat", "openai_responses"], http, False),
        ("anthropic-compatible", "Anthropic-compatible endpoint", "", ["anthropic_messages"], http, False),
        ("ollama", "Ollama", "http://127.0.0.1:11434", ["openai_chat"], OllamaInventory(), True),
        ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1", ["openai_chat", "openai_responses"], http, True),
        ("vllm", "vLLM", "http://127.0.0.1:8000/v1", ["openai_chat"], http, True),
        ("llamacpp", "llama.cpp server", "http://127.0.0.1:8080/v1", ["openai_chat"], http, True),
        ("mcp", "Stdio MCP inference", "", ["mcp"], configured, True),
        ("mock", "Deterministic test fixture", "", ["mock"], configured, True),
    ]
    result = {}
    for name, label, endpoint, protocols, inventory, local in entries:
        fields = [
            ConfigField(name="endpoint", label="Endpoint", default=endpoint),
            ConfigField(name="model_ids", label="Exact model IDs (optional)", type="list"),
        ]
        auth = ["none"] if local else ["api_key"]
        if not local:
            fields += [
                ConfigField(
                    name="api_key",
                    label="API key",
                    type="password",
                    help="Stored in your OS credential vault; never returned",
                ),
                ConfigField(name="api_key_env", label="Or environment variable name"),
            ]
        if name == "bedrock":
            fields += [
                ConfigField(name="region", label="AWS region", required=True),
                ConfigField(name="profile", label="AWS profile (optional)"),
            ]
            auth = ["aws_chain", "api_key"]
        if name == "vertex":
            fields += [
                ConfigField(name="project", label="Google Cloud project", required=True),
                ConfigField(name="region", label="Location", required=True, default="global"),
                ConfigField(name="service_account_file", label="Service account file (optional)"),
            ]
            auth = ["google_adc"]
        if name == "foundry":
            auth += ["azure_identity", "command"]
            fields += [
                ConfigField(name="options.subscription", label="Azure subscription (inventory)"),
                ConfigField(name="options.resource_group", label="Resource group (inventory)"),
                ConfigField(name="options.account", label="Account (inventory)"),
            ]
        if name == "mcp":
            fields += [
                ConfigField(name="command", label="Trusted executable", required=True),
                ConfigField(name="args", label="Arguments", type="list"),
                ConfigField(name="tool", label="Inference tool", default="code_task"),
            ]
        if name in {"openai-compatible", "anthropic-compatible"}:
            fields.append(ConfigField(name="local", label="Trusted loopback endpoint", type="boolean", default=False))
            auth.append("none")
        manifest = ProviderManifest(
            id=name,
            name=label,
            protocols=protocols,
            authentication=auth,
            fields=fields,
            default_endpoint=endpoint,
            local=local,
            pricing=name == "openrouter",
            inventory=not isinstance(inventory, ConfiguredInventory) or name == "foundry",
            native_streaming=any(p in {"openai_chat", "openai_responses", "anthropic_messages"} for p in protocols),
            description="Native credential chain; explicit model IDs" if name == "vertex" else "",
        )
        result[name] = BuiltinProvider(manifest, inventory)
    return result
