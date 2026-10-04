"""Capability/quality gates, local policy and observable model-selection reasons."""
from __future__ import annotations

import time
from .config import RolePolicy, ROLE_NAMES
from .privacy import allowed_provider, is_local
from .providers import estimate_cost
from .schema import Classification

ROLE_CAPABILITIES = {
    "classifier": ["text", "structured_output"], "arbiter": ["text", "structured_output"],
    "planner": ["code"], "executor": ["code"], "fast_executor": ["code"],
    "complex_executor": ["code"], "reviewer": ["code"], "repairer": ["code"],
    "compactor": ["text"], "summarizer": ["text"], "embedding": ["embeddings"], "vision": ["vision"],
}
ROLE_DIMENSIONS = {"planner": "planning", "reviewer": "reviewing", "repairer": "debugging", "classifier": "json_adherence",
                   "arbiter": "json_adherence", "executor": "coding", "complex_executor": "architecture", "fast_executor": "coding"}
ROLE_ALIASES = {"repair": "repairer", "fast_executor": "executor", "complex_executor": "executor"}


class RoleResolver:
    def __init__(self, settings, store, providers=None):
        self.settings, self.store, self.providers = settings, store, providers

    def priced(self, model):
        if model.input_price is not None and model.output_price is not None:
            return model
        if is_local(self.settings.providers[model.provider]):
            return model.model_copy(update={"input_price": 0.0, "output_price": 0.0, "pricing_status": "known", "pricing_source": "local_api_no_token_charge"})
        policy = self.settings.routing
        if policy.allow_unknown_pricing:
            return model.model_copy(update={"input_price": policy.unknown_input_price, "output_price": policy.unknown_output_price,
                                            "pricing_status": "estimated", "pricing_source": "user_unknown_price_allowance"})
        return model

    def select(self, decision, role="executor", capabilities=None, allow_cloud=True, excluded=None,
               min_tier=1, max_tier=4, inputs=2000, outputs=2000, budget=None):
        p = self.settings.routing
        policy_role = "repairer" if role == "repair" else role
        policy = self.settings.roles.get(policy_role, RolePolicy())
        required = list(capabilities if capabilities is not None else ROLE_CAPABILITIES.get(policy_role, []))
        eligible, explanations = [], []
        for original in self.settings.models:
            m = self.priced(original) if original.provider in self.settings.providers else original
            provider = self.settings.providers.get(m.provider)
            reasons = []
            quality = self.store.quality(m, decision.task_family)
            dimension = ROLE_DIMENSIONS.get(policy_role)
            if dimension in m.quality:
                quality = self.store.quality(m, decision.task_family, prior=m.quality[dimension])
            from .dna import estimate
            from .contracts import current_contract
            quality = estimate(self.store, m.id, dimension or decision.task_family, quality)
            quality *= m.reliability
            slo = policy.minimum_quality if policy.minimum_quality is not None else p.quality_slos.get(decision.task_family, .92)
            slo = max(slo, current_contract().get("quality") or 0)
            if policy.strategy == "disabled":
                reasons.append("ROLE_DISABLED")
            if provider is None or not provider.enabled or not m.enabled or m.status not in {"available", "degraded"}:
                reasons.append("UNAVAILABLE")
            base_role = ROLE_ALIASES.get(policy_role, policy_role)
            if base_role == "repairer":
                permitted = "repair" in m.roles or "repairer" in m.roles
            elif policy_role in {"classifier", "arbiter", "compactor", "summarizer", "embedding", "vision"}:
                permitted = policy_role in m.roles or "executor" in m.roles
            else:
                permitted = base_role in m.roles
            if not permitted:
                reasons.append("ROLE_NOT_ALLOWED")
            # Tier bounds only serve V1 callers, never automatic V2 role assignment.
            if not min_tier <= m.tier <= max_tier:
                reasons.append("LEGACY_TIER_BOUND")
            if m.id in (excluded or set()):
                reasons.append("PREVIOUSLY_FAILED")
            if not self.store.healthy(m.id):
                reasons.append("CIRCUIT_OPEN")
            health = self.store.provider_health(m.provider)
            if health["status"] in {"AUTH_REQUIRED", "RATE_LIMITED", "UNAVAILABLE"}:
                reasons.append(health["status"])
            if provider and not allowed_provider(self.settings, provider, policy_role, allow_cloud):
                reasons.append("PRIVACY_POLICY")
            if provider and not is_local(provider):
                from .contracts import tighter
                limit = tighter(current_contract().get("tokens"), current_contract().get("node_tokens"), self.settings.privacy.max_cloud_context_tokens_per_request)
                if limit is not None and inputs > limit:
                    reasons.append("EGRESS_BUDGET")
            if provider and policy.locality == "local-only" and not is_local(provider):
                reasons.append("ROLE_LOCAL_ONLY")
            if provider and policy.allowed_provider_groups and not set(policy.allowed_provider_groups).intersection(provider.groups):
                reasons.append("PROVIDER_GROUP_POLICY")
            if provider:
                from fnmatch import fnmatchcase
                if not any(fnmatchcase(m.deployment_name, pattern) for pattern in provider.include_models) or any(fnmatchcase(m.deployment_name, pattern) for pattern in provider.exclude_models):
                    reasons.append("MODEL_FILTER_POLICY")
            if not m.supports(required):
                reasons.extend("MISSING_" + c.upper() for c in required if not m.supports([c]))
            if m.input_price is None or m.output_price is None:
                reasons.append("PRICE_UNKNOWN")
            if m.pricing_updated_at and time.time() - m.pricing_updated_at > p.price_max_age_days * 86400 and not p.allow_stale_pricing and provider and not is_local(provider):
                reasons.append("PRICE_STALE")
            if role == "planner" and p.planner_model_candidates and m.id not in p.planner_model_candidates:
                reasons.append("PLANNER_ALLOWLIST")
            if quality < slo and (not p.allow_below_slo or current_contract().get("quality")):
                reasons.append("BELOW_QUALITY_SLO")
            if inputs + min(outputs, m.max_output) > m.context_window:
                reasons.append("CONTEXT_TOO_SMALL")
            if provider and is_local(provider) and p.resource_aware and p.available_ram_mb is not None and m.ram_estimate_mb and not m.loaded and m.ram_estimate_mb > p.available_ram_mb * .85:
                reasons.append("MEMORY_PRESSURE")
            if self.providers:
                window = self.providers.windows.get(m.id, [])
                if sum(stamp > time.monotonic() - 60 for stamp in window) >= m.rate_limit:
                    reasons.append("RATE_LIMITED")
            cost = None if m.input_price is None or m.output_price is None else estimate_cost(m, inputs, min(outputs, m.max_output), reserve=True)
            if cost is not None and budget is not None and cost > budget:
                reasons.append("BUDGET_EXCEEDED")
            row = {"model": m.id, "eligible": not reasons, "reason_codes": reasons, "quality_estimate": round(quality, 4), "quality_target": slo,
                   "estimated_max_cost": cost, "latency_estimate": m.expected_latency, "local": bool(provider and is_local(provider)),
                   "pricing_status": m.pricing_status, "capabilities": required}
            explanations.append(row)
            if not reasons:
                utility = quality - p.latency_weight * min(m.expected_latency/180, 1)
                economic_evidence = estimate(self.store, m.id, "cost_efficiency", .5)
                row["verified_cost_utility"] = round((economic_evidence-.5)*p.historical_success_bonus, 6)
                utility += row["verified_cost_utility"]
                if provider and is_local(provider) and (p.local_first or policy.locality == "local-preferred"):
                    utility += p.local_execution_bonus
                if m.loaded:
                    utility += .01
                eligible.append((m, cost, utility, row))
        # Exact Pareto membership in O(n log n), with duplicate points grouped.
        from bisect import bisect_left
        points = {(item[1], item[0].expected_latency, item[3]["quality_estimate"]) for item in eligible}
        latencies = sorted({point[1] for point in points})
        tree, frontier = [-1.0] * (len(latencies) + 1), set()
        for cost, latency, quality in sorted(points, key=lambda point: (point[0], point[1], -point[2])):
            index = bisect_left(latencies, latency) + 1
            prior, cursor = -1.0, index
            while cursor:
                prior = max(prior, tree[cursor])
                cursor -= cursor & -cursor
            if prior < quality:
                frontier.add((cost, latency, quality))
            while index < len(tree):
                tree[index] = max(tree[index], quality)
                index += index & -index
        for item in eligible:
            row = item[3]
            row["pareto_frontier"] = (item[1], item[0].expected_latency, row["quality_estimate"]) in frontier
        eligible.sort(key=lambda item: (item[1]/(1+item[3].get("verified_cost_utility", 0)), -item[2], item[0].id))
        pinned = policy.model
        if pinned and policy.strategy in {"preferred", "pinned"}:
            eligible.sort(key=lambda item: item[0].id != pinned)
        notice = None
        if pinned and not any(item[0].id == pinned for item in eligible):
            notice = "Pinned model unavailable. " + ("Using fallback " + eligible[0][0].id + "." if eligible else "No eligible fallback.")
        models = [item[0] for item in eligible]
        return models, {"role": policy_role, "strategy": policy.strategy, "selected": models[0].id if models else None,
                        "notice": notice, "candidates": explanations, "fallback_graph": self.fallback_graph(models, required)}

    @staticmethod
    def fallback_graph(models, required):
        return {"nodes": [m.id for m in models], "edges": [{"from": left.id, "to": right.id, "required_capabilities": required,
                "reason": "NEXT_ELIGIBLE_CANDIDATE"} for left, right in zip(models, models[1:])]}

    def resolve(self, role, decision=None, **kwargs):
        _, explanation = self.select(decision or Classification(task_family="explanation" if role in {"classifier", "arbiter", "compactor", "summarizer", "embedding", "vision"} else "coding"), role, **kwargs)
        if role in {"classifier", "arbiter"} and explanation["selected"] is None:
            explanation["fallback"] = "local deterministic policy"
        return explanation

    def all(self, **kwargs):
        return {role: self.resolve(role, **kwargs) for role in ROLE_NAMES}
