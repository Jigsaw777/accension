"""Versioned provider extension API. Plugins are explicitly trusted Python code."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Literal, Protocol

from pydantic import Field

from .errors import CapabilityUnsupported, ProviderUnavailable
from .schema import ModelDescriptor, Provider, Strict

PROVIDER_PLUGIN_API_VERSION = "1"


class ConfigField(Strict):
    name: str
    label: str
    type: Literal["text", "password", "select", "list", "boolean"] = "text"
    required: bool = False
    default: str | bool = ""
    choices: list[str] = []
    help: str = ""


class ProviderManifest(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str
    plugin_api_version: Literal["1"] = PROVIDER_PLUGIN_API_VERSION
    protocols: list[str] = Field(min_length=1)
    authentication: list[str] = ["api_key"]
    fields: list[ConfigField] = []
    default_endpoint: str = ""
    local: bool = False
    inventory: bool = True
    pricing: bool = False
    health: bool = True
    native_streaming: bool = False
    description: str = ""


class InventoryResult(Strict):
    models: list[ModelDescriptor] = []
    complete: bool = True
    source: str = "provider_inventory"
    warnings: list[str] = []


class TransportAdapter(Protocol):
    async def generate(self, context, model, messages, maximum, schema=None, **kwargs): ...


@dataclass
class ProviderContext:
    name: str
    provider: Provider
    manager: object

    @property
    def client(self):
        return self.manager.client

    @property
    def settings(self):
        return self.manager.settings

    @property
    def auth(self):
        return self.manager.auth

    async def headers(self):
        return await self.auth.headers(self.provider)

    def descriptor(self, remote_id, **kwargs):
        import time

        from .privacy import is_local

        local = is_local(self.provider)
        defaults = {
            "id": self.name + ":" + remote_id,
            "provider": self.name,
            "deployment_name": remote_id,
            "locality": "local" if local else "cloud",
            "discovered_from": self.provider.kind,
            "discovered_at": time.time(),
            "capabilities_source": "conservative_unknown",
            "supports_code": False,
            "supports_chat_completions": False,
            "protocols": [self.manager.protocol(self.provider)],
        }
        if local:
            defaults.update(
                input_price=0,
                output_price=0,
                pricing_status="known",
                pricing_source="local_api_no_token_charge",
                local_runtime=self.provider.kind,
            )
        defaults.update(kwargs)
        for name, value in defaults.pop("capabilities", {}).items():
            defaults["supports_" + name] = value
        return ModelDescriptor.model_validate(defaults)


class ProviderPlugin:
    plugin_api_version = PROVIDER_PLUGIN_API_VERSION

    def manifest(self) -> ProviderManifest:
        raise NotImplementedError

    def auth_schema(self):
        return [field.model_dump() for field in self.manifest().fields]

    async def discover_models(self, context: ProviderContext) -> InventoryResult:
        return InventoryResult(
            models=[
                context.descriptor(model, status="available", discovered_from="explicit_configuration")
                for model in context.provider.model_ids
            ],
            complete=False,
            source="explicit_configuration",
            warnings=["Inventory unavailable; configured model IDs preserved"],
        )

    async def pricing(self, context):
        return {}

    async def health(self, context):
        inventory = await self.discover_models(context)
        return {
            "status": "ok",
            "models": [m.deployment_name for m in inventory.models],
            "inventory_complete": inventory.complete,
        }

    async def probe_model(self, context, model, capability, budget=0, approved=False):
        from .calibration import probe_model

        return await probe_model(context.manager, model, capability, budget, approved)

    def create_transport(self, protocol: str) -> TransportAdapter:
        from .transports import TRANSPORTS

        if protocol not in self.manifest().protocols or protocol not in TRANSPORTS:
            raise CapabilityUnsupported("Provider does not support configured transport")
        return TRANSPORTS[protocol]()


class PluginRegistry:
    def __init__(self, enabled=()):
        from .builtin_providers import builtins

        self.plugins = builtins()
        self.errors = {}
        installed = entry_points(group="accension.providers")
        self.available = {entry.name: entry for entry in installed}
        for name in enabled:
            try:
                if name in self.plugins:
                    raise ValueError("Cannot replace a built-in plugin")
                entry = self.available[name]
                plugin = entry.load()()
                if plugin.plugin_api_version != PROVIDER_PLUGIN_API_VERSION:
                    raise ValueError("Incompatible plugin API")
                manifest = ProviderManifest.model_validate(plugin.manifest())
                if manifest.id != name:
                    raise ValueError("Plugin ID differs from entry point")
                self.plugins[name] = plugin
            except Exception as exc:
                # A broken plugin must not prevent configuration mode from starting.
                self.errors[name] = type(exc).__name__

    def get(self, kind):
        if kind not in self.plugins:
            raise ProviderUnavailable("Provider plugin unavailable or not explicitly trusted: " + kind)
        return self.plugins[kind]

    def manifests(self):
        return [plugin.manifest().model_dump() for plugin in self.plugins.values()]
