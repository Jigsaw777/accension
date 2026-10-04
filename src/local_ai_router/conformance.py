"""Reusable provider checks. Generation is opt-in and tightly budgeted."""

import math

from .errors import normalize_error
from .privacy import is_local
from .provider_sdk import ProviderManifest


async def check_provider(engine, name, inference=False, budget=0.01, allow_paid=False):
    if name not in engine.settings.providers:
        raise ValueError("Unknown configured provider")
    if not math.isfinite(budget) or not 0 <= budget <= engine.settings.budgets.default_request_budget:
        raise ValueError("Conformance budget must fit the request limit")
    provider = engine.settings.providers[name]
    plugin = engine.providers.plugins.get(provider.kind)
    manifest = ProviderManifest.model_validate(plugin.manifest().model_dump())
    checks = [{"check": "manifest", "status": "passed", "api_version": manifest.plugin_api_version}]
    checks.append(
        {"check": "credential_schema", "status": "passed" if isinstance(plugin.auth_schema(), list) else "failed"}
    )
    health = await engine.providers.health(provider)
    checks.append({"check": "health", "status": "passed" if health["status"] == "ok" else "blocked", "details": health})
    try:
        inventory = await engine.providers.discover(name, provider)
        checks.append(
            {"check": "inventory", "status": "passed", "models": len(inventory.models), "complete": inventory.complete}
        )
        engine.discovery.reconcile(name, inventory)
    except Exception as exc:
        checks.append({"check": "inventory", "status": "blocked", "error": normalize_error(exc).code})
    models = [m for m in engine.settings.models if m.provider == name and m.enabled]
    if inference and models:
        if not is_local(provider) and not allow_paid:
            raise ValueError("Cloud conformance requires --allow-paid after reviewing --budget")
        from .calibration import probe_model

        model = models[0]
        capabilities = ["text"] + [c for c in ("structured_output", "streaming", "tools") if model.supports([c])]
        for capability in capabilities:
            try:
                evidence = await probe_model(
                    engine.providers, model, capability, budget / len(capabilities), allow_paid
                )
                checks.append(
                    {
                        "check": capability,
                        "status": "passed" if evidence["supported"] else "failed",
                        "evidence": evidence,
                    }
                )
            except Exception as exc:
                checks.append({"check": capability, "status": "blocked", "error": normalize_error(exc).code})
    else:
        checks.append(
            {"check": "generation", "status": "not_run", "reason": "Use --inference with one-model bounded budget"}
        )
    return {
        "provider": name,
        "checks": checks,
        "maximum_inference_budget": budget if inference else 0,
        "note": "Authentication failures, usage, model mapping, error normalization and secret safety are also covered by the offline conformance test fixtures.",
    }
