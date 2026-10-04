from __future__ import annotations

import asyncio
import json
import os
from typing import Protocol

from .decision import ArbitrationPolicy, LocalDecisionEngine
from .decision import deterministic as deterministic
from .migration import classifier_reason
from .privacy import allowed_provider
from .providers import ProviderError, parse_json
from .roles import RoleResolver
from .schema import Arbitration, Classification
from .store import cache_key


class SemanticClassifier(Protocol):
    async def classify(self, text: str) -> dict: ...
    async def close(self) -> None: ...


class MCPSemanticClassifier:
    """One lazily started MCP child; its heavy checkpoint is reused between requests."""

    def __init__(self, policy):
        self.policy = policy
        self.queue = asyncio.Queue(maxsize=4)
        self.task = None

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {
            k: v
            for k, v in os.environ.items()
            if not any(word in k.lower() for word in ("api_key", "secret", "token", "password", "credential"))
        }
        params = StdioServerParameters(
            command=self.policy.classifier_command, args=self.policy.classifier_args, env=env
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    while True:
                        state, future = await self.queue.get()
                        if future.cancelled():
                            continue
                        try:
                            result = await session.call_tool(
                                self.policy.classifier_tool,
                                {
                                    "state": {"request": state[:3000]},
                                    "min_confidence": self.policy.classifier_confidence_threshold,
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "task_family": {
                                                "type": "string",
                                                "enum": ["simple", "coding", "architecture", "critical", "explanation"],
                                            },
                                            "planning_required": {"type": "boolean"},
                                            "complexity": {"type": "integer", "minimum": 0, "maximum": 100},
                                            "risk": {"type": "integer", "minimum": 0, "maximum": 100},
                                        },
                                    },
                                },
                            )
                            data = json.loads(next(x.text for x in result.content if x.type == "text"))
                            if not future.done():
                                future.set_result(data)
                        except Exception as exc:
                            if not future.done():
                                future.set_exception(ProviderError(type(exc).__name__))
        except asyncio.CancelledError:
            raise
        except Exception:
            while not self.queue.empty():
                _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(ProviderError("Optional classifier unavailable"))

    async def classify(self, text):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._serve())
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((text, future))
        return await asyncio.wait_for(future, self.policy.classifier_timeout)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class Router:
    def __init__(self, settings, store, providers):
        self.settings, self.store, self.providers = settings, store, providers
        self.classifier = MCPSemanticClassifier(settings.routing)
        self.decision_engine = LocalDecisionEngine()
        self.arbitration = ArbitrationPolicy(settings.routing)
        self.roles = RoleResolver(settings, store, providers)

    @property
    def laya(self):
        """Deprecated V1 Python alias."""
        return self.classifier

    async def classify(self, task, request_id, graph=None):
        key = cache_key("classification-v2", task, self.settings.fingerprint(), getattr(graph, "fingerprint", None))
        cached = self.store.get(key)
        if cached:
            self.store.trace(request_id, "classification_cache", hit=True)
            return Classification.model_validate(cached)
        decision = self.decision_engine.decide(task, graph)
        policy = self.settings.routing
        role = self.settings.roles.get("classifier")
        if (
            policy.classifier_enabled
            and (not role or role.strategy != "disabled")
            and decision.task_family not in {"simple", "explanation"}
        ):
            try:
                if policy.classifier_command:
                    data = await self.classifier.classify(task)
                else:
                    candidates, _ = self.roles.select(
                        decision,
                        "classifier",
                        budget=self.settings.budgets.default_request_budget,
                        inputs=1500,
                        outputs=256,
                    )
                    if not candidates:
                        raise ProviderError("No eligible optional classifier")
                    result = await self.providers.generate(
                        candidates[0],
                        [
                            {
                                "role": "system",
                                "content": 'Classify the task. Return JSON {"values":{"task_family":"simple|coding|architecture|critical|explanation","complexity":0,"risk":0,"planning_required":false},"confidence":{"classification":0.0}}. Scores 0-100, confidence 0-1.',
                            },
                            {"role": "user", "content": task[:3000]},
                        ],
                        "classifier",
                        request_id,
                        "classification",
                        self.settings.budgets.default_request_budget,
                        max_output=256,
                    )
                    data = parse_json(result.text)
                values, confidences = data["values"], data["confidence"]
                confidence = min(confidences.values())
                if confidence >= policy.classifier_confidence_threshold and all(v is not None for v in values.values()):
                    candidate = Classification.model_validate(
                        {
                            **decision.model_dump(),
                            **values,
                            "confidence": confidence,
                            "reason_codes": classifier_reason("ACCEPTED"),
                            "decision_source": decision.decision_source + ["optional_local_classifier"],
                        }
                    )
                    # A classifier cannot downgrade deterministic critical risk.
                    if decision.task_family == "critical":
                        candidate.risk = max(candidate.risk, decision.risk)
                        candidate.task_family = "critical"
                        candidate.planning_required = True
                    decision = candidate
                else:
                    decision.reason_codes.extend(classifier_reason("ABSTAINED"))
            except Exception:
                decision.reason_codes.extend(classifier_reason("TIMEOUT_OR_UNAVAILABLE"))
        decision.requires_frontier_planner = decision.complexity >= policy.frontier_planner_complexity_threshold
        decision.planning_required |= decision.requires_frontier_planner
        self.store.trace(request_id, "classification", **decision.model_dump())
        self.store.put(key, decision.model_dump())
        return decision

    def needs_arbiter(self, c, failures=0):
        return self.arbitration.needed(c, failures)

    def needs_jev(self, c, failures=0):
        """Deprecated V1 Python alias."""
        return self.needs_arbiter(c, failures)

    async def arbitrate(self, c, request_id, session, budget, candidates, failures=0, allow_cloud=True):
        if not self.needs_arbiter(c, failures):
            return None
        role_policy = self.settings.roles.get("arbiter")
        if role_policy and role_policy.strategy == "disabled":
            return None
        configured = (role_policy.model if role_policy else None) or self.settings.routing.arbiter_model
        eligible, _ = self.roles.select(
            c, "arbiter", ["text", "structured_output"], allow_cloud=allow_cloud, budget=budget
        )
        model = next((m for m in eligible if m.id == configured), None)
        if model and not allowed_provider(
            self.settings, self.settings.providers[model.provider], "arbiter", allow_cloud
        ):
            self.store.trace(request_id, "arbitration", status="deterministic", reason_code="LOCAL_CONTROL_POLICY")
            return None
        if model is None:
            self.store.trace(request_id, "arbitration", status="deterministic", reason_code="NO_CONFIGURED_ARBITER")
            return None
        payload = {
            "classification": c.model_dump(),
            "candidate_ids": [m.id for m in candidates],
            "failures": failures,
            "budget": budget,
        }
        messages = [
            {
                "role": "system",
                "content": "Return routing arbitration JSON only. Schema: "
                + json.dumps(Arbitration.model_json_schema()),
            },
            {"role": "user", "content": json.dumps(payload)},
        ]
        from .store import BudgetExceeded

        try:
            result = await self.providers.generate(
                model, messages, "arbiter", request_id, session, budget, schema=Arbitration.model_json_schema()
            )
            decision = Arbitration.model_validate(parse_json(result.text))
        except (ProviderError, ValueError, BudgetExceeded) as exc:
            self.store.trace(
                request_id,
                "arbitration",
                status="deterministic",
                reason_code="OPTIONAL_ARBITER_UNAVAILABLE",
                error_type=type(exc).__name__,
            )
            return None
        if decision.model_id and decision.model_id not in payload["candidate_ids"]:
            raise ProviderError("Arbiter selected an ineligible model")
        self.store.trace(request_id, "arbitration", **decision.model_dump())
        if decision.reason_code in {"NEEDS_HUMAN_APPROVAL", "STOP_ESCALATION"}:
            raise ProviderError(decision.reason_code)
        return decision

    def candidates(
        self, c, role="executor", capabilities=None, allow_cloud=True, excluded=None, min_tier=1, max_tier=4, **kwargs
    ):
        return self.roles.select(c, role, capabilities or [], allow_cloud, excluded, min_tier, max_tier, **kwargs)[0]


LayaClient = MCPSemanticClassifier  # Deprecated import alias.
