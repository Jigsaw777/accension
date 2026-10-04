"""Explicit model inventory with the built-in OpenAI Chat transport.

This example is trusted Python code, not a sandbox. It does not invent model
capabilities or claim that a configured deployment is reachable.
"""
from local_ai_router.provider_sdk import (
    ConfigField, InventoryResult, ProviderManifest, ProviderPlugin,
)


class ExampleProvider(ProviderPlugin):
    plugin_api_version = "1"

    def manifest(self):
        return ProviderManifest(
            id="example-loopback",
            name="Example loopback provider",
            protocols=["openai_chat"],
            authentication=["none"],
            local=True,
            inventory=False,
            default_endpoint="http://127.0.0.1:9000/v1",
            fields=[
                ConfigField(name="endpoint", label="Loopback endpoint",
                            default="http://127.0.0.1:9000/v1", required=True),
                ConfigField(name="model_ids", label="Exact loaded model IDs",
                            type="list", required=True),
            ],
        )

    async def discover_models(self, context):
        return InventoryResult(
            models=[context.descriptor(
                model_id, status="unknown", supports_chat_completions=True,
                discovered_from="explicit_configuration",
            ) for model_id in context.provider.model_ids],
            complete=False,
            source="explicit_configuration",
            warnings=["Configured IDs are not verified deployments; test before use."],
        )

    async def health(self, context):
        # Unlike a configured inventory, this check actually contacts the server.
        response = await context.client.get(
            context.provider.endpoint.rstrip("/") + "/models",
            headers=await context.headers(),
            timeout=context.settings.discovery.probe_timeout,
        )
        response.raise_for_status()
        return {"status": "ok", "inventory_complete": False}
