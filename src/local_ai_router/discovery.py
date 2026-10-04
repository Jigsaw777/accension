"""Bounded, loopback-only detection and durable provider inventory reconciliation."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from .errors import normalize_error
from .privacy import allowed_provider, is_local
from .schema import Model, Provider


class DiscoveryService:
    def __init__(self, engine):
        self.engine = engine
        self.last = 0.0
        self.lock = asyncio.Lock()
        self.status = {}

    async def discover_local(self):
        s = self.engine.settings

        async def probe(name, endpoint):
            kind = "openai-compatible" if name == "local-openai" else name
            provider = Provider(kind=kind, endpoint=endpoint, local=True, auth="none")
            from .config import validate_endpoint

            validate_endpoint(provider, s.port)
            key = "detected-" + name
            try:
                inventory = await self.engine.providers.discover(key, provider)
                return key, provider, inventory, None
            except Exception as exc:
                return key, provider, None, normalize_error(exc).code

        results = await asyncio.gather(
            *(probe(name, endpoint) for name, endpoint in s.discovery.local_endpoints.items())
        )
        services = []
        for name, provider, inventory, error in results:
            if inventory is not None:
                existing = next(
                    (
                        key
                        for key, value in s.providers.items()
                        if value.endpoint.rstrip("/") == provider.endpoint.rstrip("/")
                    ),
                    None,
                )
                if existing:
                    name = existing
                    if not s.providers[name].enabled:
                        services.append(
                            {"provider": name, "status": "detected_disabled", "models": len(inventory.models)}
                        )
                        continue
                    for model in inventory.models:
                        model.provider = name
                        model.id = name + ":" + model.deployment_name
                else:
                    s.providers[name] = provider
                self.reconcile(name, inventory)
            services.append(
                {
                    "provider": name,
                    "status": "detected" if inventory is not None else "not_detected",
                    "models": len(inventory.models) if inventory else 0,
                    "error": error,
                }
            )
        self.engine.store.metadata(
            "detected-providers",
            {key: value.model_dump() for key, value in s.providers.items() if key.startswith("detected-")},
        )
        self.engine.store.metadata("local-services", services)
        return {"services": services, "scope": "loopback endpoints only"}

    def reconcile(self, provider_name, inventory):
        with self.engine.store.transaction():
            return self._reconcile(provider_name, inventory)

    def _reconcile(self, provider_name, inventory):
        s, store = self.engine.settings, self.engine.store
        configured_ids = set(getattr(self.engine, "configured_model_ids", set()))
        existing = {m.id: m for m in s.models}
        positions = {m.id: i for i, m in enumerate(s.models)}
        configured = {(m.provider, m.deployment_name): m for m in s.models if m.id in configured_ids}
        active, added = set(), []
        for discovered in inventory.models:
            explicit = configured.get((provider_name, discovered.deployment_name))
            if explicit:
                active.add(explicit.id)
                explicit.inventory_stale = False
                explicit.status = discovered.status
                store.save_model(explicit)
                continue
            model_id = provider_name + ":" + discovered.deployment_name
            active.add(model_id)
            old = existing.get(model_id)
            values = {
                **s.discovery.model_defaults,
                **discovered.model_dump(exclude_unset=True),
                "id": model_id,
                "provider": provider_name,
            }
            if old:
                values.update(
                    quality=old.quality, benchmarked_at=old.benchmarked_at, capability_evidence=old.capability_evidence
                )
                for capability, evidence in old.capability_evidence.items():
                    if evidence.get("source") in {"active_probe", "user_override"}:
                        values["supports_" + capability] = bool(evidence.get("supported"))
                if old.pricing_source == "user_supplied":
                    for key in (
                        "input_price",
                        "output_price",
                        "cached_input_price",
                        "cache_write_price",
                        "pricing_status",
                        "pricing_source",
                        "pricing_updated_at",
                    ):
                        values[key] = getattr(old, key)
            if is_local(s.providers[provider_name]):
                values.update(tier=1, input_price=0, output_price=0)
            values.update(s.discovery.overrides.get(discovered.deployment_name, {}))
            values.update(s.discovery.overrides.get(model_id, {}))
            model = Model.model_validate(values)
            model.inventory_stale = False
            if old:
                s.models[positions[model.id]] = model
            else:
                positions[model.id] = len(s.models)
                s.models.append(model)
                added.append(model.id)
            existing[model.id] = model
            self.engine.providers.semaphores.setdefault(model.id, asyncio.Semaphore(model.concurrency_limit))
            self.engine.providers.windows.setdefault(model.id, deque())
            store.save_model(model)
        if inventory.complete:
            for model in s.models:
                if model.provider == provider_name and model.id not in active:
                    model.status = "removed"
                    if model.id not in configured_ids:
                        model.enabled = False
                    store.save_model(model)
        return added

    async def refresh(self, force=False, provider_name=None):
        s, policy, store = self.engine.settings, self.engine.settings.discovery, self.engine.store
        if not policy.enabled or s.mock:
            return {"status": "disabled"}
        async with self.lock:
            if not force and time.monotonic() - self.last < policy.interval_seconds:
                return self.status
            self.last = time.monotonic()
            services = None
            if (
                policy.local_scan
                and not policy.inventory_providers
                and not any(p.enabled and p.local for p in s.providers.values())
            ):
                services = await self.discover_local()
            names = set(policy.inventory_providers)
            names.update(name for name, provider in s.providers.items() if provider.enabled and provider.inventory)
            if provider_name:
                if provider_name not in s.providers:
                    raise ValueError("Unknown provider instance")
                names = {provider_name}
            status, added, warnings = {}, [], []

            async def refresh_one(name):
                provider = s.providers.get(name)
                if not provider or not provider.enabled:
                    return
                if not allowed_provider(s, provider, "metadata"):
                    status[name] = "privacy_blocked"
                    return
                try:
                    inventory = await self.engine.providers.discover(name, provider)
                    added.extend(self.reconcile(name, inventory))
                    status[name] = "ok"
                    warnings.extend({"provider": name, "message": message} for message in inventory.warnings)
                    if store.provider_health(name)["status"] == "AUTH_REQUIRED":
                        store.provider_health(name, "UNKNOWN")
                except Exception as exc:
                    error = normalize_error(exc)
                    status[name] = error.code
                    for model in s.models:
                        if model.provider == name:
                            model.inventory_stale = True
                            store.save_model(model)

            await asyncio.gather(*(refresh_one(name) for name in sorted(names)))
            self.status = {
                "providers": status,
                "added": added,
                "warnings": warnings,
                "local": services,
                "pricing_policy": "Unknown cloud prices excluded unless explicitly permitted with a reservation allowance",
            }
            store.metadata("discovery-status", self.status)
            return self.status

    async def watch(self):
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = {"error": type(exc).__name__}
            await asyncio.sleep(self.engine.settings.discovery.interval_seconds)
