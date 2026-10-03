from __future__ import annotations
import asyncio, json, re
from .schema import Classification, Arbitration
from .providers import ProviderError, estimate_cost, parse_json
from .store import cache_key

def deterministic(task: str) -> Classification:
    text = task.lower()
    critical = bool(re.search(r"security|credential|authenticat|migration|concurren|race condition|deadlock|encrypt", text))
    architecture = bool(re.search(r"architect|multi.module|across|redesign|feature|api.*database", text))
    simple = bool(re.search(r"typo|rename|format|documentation|explain|unit test", text))
    family = "critical" if critical else "architecture" if architecture else "explanation" if "explain" in text else "simple" if simple else "coding"
    return Classification(task_family=family, complexity=80 if architecture or critical else 10 if simple else 35,
                          risk=85 if critical else 45 if architecture else 10 if simple else 25,
                          planning_required=architecture or critical, requires_frontier_planner=architecture or critical,
                          recommended_tier=4 if architecture or critical else 1, confidence=.9 if simple or critical else .7,
                          estimated_files=8 if architecture else 1, reason_codes=["DETERMINISTIC_PRIOR"])

class LayaClient:
    """One lazily started MCP child; its heavy checkpoint is reused between requests."""
    def __init__(self, policy):
        self.policy = policy
        self.queue = asyncio.Queue(maxsize=4)
        self.task = None

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=self.policy.laya_command, args=self.policy.laya_args)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    while True:
                        state, future = await self.queue.get()
                        if future.cancelled():
                            continue
                        try:
                            result = await session.call_tool(self.policy.classifier_tool, {"state": {"request": state[:3000]}, "min_confidence": self.policy.laya_confidence_threshold,
                                "schema": {"type": "object", "properties": {"task_family": {"type": "string", "enum": ["simple", "coding", "architecture", "critical", "explanation"]},
                                "planning_required": {"type": "boolean"}, "complexity": {"type": "integer", "minimum": 0, "maximum": 100}, "risk": {"type": "integer", "minimum": 0, "maximum": 100}}}})
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
                    future.set_exception(ProviderError("Laya unavailable"))

    async def classify(self, text):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._serve())
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((text, future))
        return await asyncio.wait_for(future, self.policy.laya_timeout)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

class Router:
    def __init__(self, settings, store, providers):
        self.settings, self.store, self.providers = settings, store, providers
        self.laya = LayaClient(settings.routing)

    async def classify(self, task, request_id):
        key = cache_key("classification-v1", task, self.settings.fingerprint())
        cached = self.store.get(key)
        if cached:
            self.store.trace(request_id, "classification_cache", hit=True)
            return Classification.model_validate(cached)
        decision = deterministic(task)
        policy = self.settings.routing
        if policy.laya_enabled and policy.laya_command and decision.task_family not in {"simple", "explanation"}:
            try:
                data = await self.laya.classify(task)
                values, confidences = data["values"], data["confidence"]
                confidence = min(confidences.values())
                if confidence >= policy.laya_confidence_threshold and all(v is not None for v in values.values()):
                    candidate = Classification.model_validate({**decision.model_dump(), **values, "confidence": confidence, "reason_codes": ["LAYA_ACCEPTED"]})
                    # A classifier cannot downgrade deterministic critical risk.
                    if decision.task_family == "critical":
                        candidate.risk = max(candidate.risk, decision.risk)
                        candidate.task_family = "critical"
                        candidate.planning_required = True
                    decision = candidate
                else:
                    decision.reason_codes.append("LAYA_ABSTAINED")
            except Exception:
                decision.reason_codes.append("LAYA_TIMEOUT_OR_UNAVAILABLE")
        decision.requires_frontier_planner = decision.complexity >= policy.frontier_planner_complexity_threshold
        decision.planning_required |= decision.requires_frontier_planner
        self.store.trace(request_id, "classification", **decision.model_dump())
        self.store.put(key, decision.model_dump())
        return decision

    def needs_jev(self, c, failures=0):
        p = self.settings.routing
        return c.confidence < p.laya_confidence_threshold or c.risk >= p.jev_risk_threshold or c.task_family in {"architecture", "critical"} or failures >= 2

    async def arbitrate(self, c, request_id, session, budget, candidates, failures=0):
        if not self.needs_jev(c, failures):
            return None
        model = next((m for m in self.settings.models if m.id == self.settings.routing.jev_model and m.enabled), None)
        if model is None:
            self.store.trace(request_id, "jev", status="unavailable", reason_code="NO_CONFIGURED_DEPLOYMENT")
            if c.risk >= self.settings.routing.jev_risk_threshold and self.settings.routing.require_jev_for_critical:
                raise ProviderError("Critical work requires Jev; configure routing.jev_model or an explicit policy override")
            return None
        payload = {"classification": c.model_dump(), "candidate_ids": [m.id for m in candidates], "failures": failures, "budget": budget}
        messages = [{"role": "system", "content": "Return routing arbitration JSON only. Schema: "+json.dumps(Arbitration.model_json_schema())}, {"role": "user", "content": json.dumps(payload)}]
        result = await self.providers.generate(model, messages, "arbiter", request_id, session, budget, schema=Arbitration.model_json_schema())
        decision = Arbitration.model_validate(parse_json(result.text))
        if decision.model_id and decision.model_id not in payload["candidate_ids"]:
            raise ProviderError("Jev selected an ineligible model")
        self.store.trace(request_id, "jev", **decision.model_dump())
        if decision.reason_code in {"NEEDS_HUMAN_APPROVAL", "STOP_ESCALATION"}:
            raise ProviderError(decision.reason_code)
        return decision

    def candidates(self, c, role="executor", capabilities=None, allow_cloud=True, excluded=None, min_tier=1, max_tier=4):
        p = self.settings.routing
        candidates = []
        for m in self.settings.models:
            provider = self.settings.providers[m.provider]
            if not m.enabled or not provider.enabled or role not in m.roles or not min_tier <= m.tier <= max_tier:
                continue
            if m.id in (excluded or set()) or not self.store.healthy(m.id) or (not allow_cloud and not provider.local):
                continue
            if not m.supports(capabilities or []) or m.input_price is None or m.output_price is None:
                continue
            if role == "planner" and p.planner_model_candidates and m.id not in p.planner_model_candidates:
                continue
            quality = self.store.quality(m, c.task_family)
            slo = p.quality_slos.get(c.task_family, .92)
            if quality < slo and not p.allow_below_slo:
                continue
            cost = estimate_cost(m, 2000, 2000)
            utility = quality - p.cost_weight*cost - p.latency_weight*min(m.expected_latency/180,1) - p.failure_risk_weight*(1-quality)
            utility += p.local_execution_bonus if p.local_first and provider.local else 0
            candidates.append((m, cost, utility))
        # Quality is a gate, cost is primary; utility resolves equal-cost candidates.
        candidates.sort(key=lambda x: (x[1], -x[2]))
        return [m for m, _, _ in candidates]
