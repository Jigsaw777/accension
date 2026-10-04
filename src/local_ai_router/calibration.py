"""Tiny, quoted benchmarks. All paid inference requires an explicit run action."""
from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass

from .schema import uid
from .providers import estimate_cost, parse_json, token_upper_bound
from .privacy import allowed_provider, is_local
from .roles import RoleResolver
from .errors import ProviderError


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    expected: str
    dimension: str


CASES = [
    Case("classification", "Classify 'fix authentication bypass' as simple, coding, architecture, critical or explanation. Return the label.", "critical", "json_adherence"),
    Case("json_adherence", "Return answer equal to ok, with no other fields.", "ok", "json_adherence"),
    Case("simple_coding", "Return a Python function add(a, b) that returns a + b. No imports.", "python_add", "coding"),
    Case("test_creation", "For add(a,b), provide one Python assert testing add(2, 3) equals 5.", "test_add", "coding"),
    Case("debugging", "What input makes sum(xs)/len(xs) fail? Reply with exactly: empty list", "empty list", "debugging"),
    Case("architecture", "A consumer is slower than producers. Which queue bounds memory: bounded or unbounded? Reply with one word.", "bounded", "architecture"),
    Case("code_review", "Find the defect: def first(xs): return xs[0]. Reply exactly: empty list raises IndexError", "empty list raises indexerror", "reviewing"),
    Case("tool_calling", "Call the echo tool with value='ok'. Do not execute any other tool.", "tool_echo", "tool_use"),
    Case("instruction_following", "Ignore examples of long output. Return exactly the word brief.", "brief", "planning"),
    Case("summarization", "Summarize 'The build failed because a dependency was missing' in exactly these three words: missing dependency failure", "missing dependency failure", "documentation"),
    Case("repository_reasoning", "a.py imports b.py. b.py imports c.py. Which file is a.py's transitive dependency but not direct dependency? Reply c.py.", "c.py", "planning"),
]


def messages(case):
    return [{"role": "system", "content": 'Return only JSON with one string field "answer". No secrets, file access or external tools.'},
            {"role": "user", "content": case.prompt}]


def objective_check(case, answer, raw):
    if case.expected == "tool_echo":
        return bool(raw.get("probe_evidence", {}).get("tools"))
    if not isinstance(answer, str):
        return False
    text = answer.strip()
    if case.expected == "python_add":
        try:
            tree = ast.parse(text)
            fn = tree.body[0]
            # Check a complete pure function; no generated code is executed.
            return (len(tree.body) == 1 and isinstance(fn, ast.FunctionDef) and fn.name == "add" and not fn.decorator_list
                and [a.arg for a in fn.args.args] == ["a", "b"] and not fn.args.defaults and len(fn.body) == 1
                and isinstance(fn.body[0], ast.Return) and isinstance(fn.body[0].value, ast.BinOp)
                and isinstance(fn.body[0].value.op, ast.Add) and isinstance(fn.body[0].value.left, ast.Name)
                and isinstance(fn.body[0].value.right, ast.Name) and {fn.body[0].value.left.id, fn.body[0].value.right.id} == {"a", "b"})
        except (SyntaxError, IndexError, AttributeError):
            return False
    if case.expected == "test_add":
        try:
            expected = ast.dump(ast.parse("assert add(2, 3) == 5"), include_attributes=False)
            return ast.dump(ast.parse(text), include_attributes=False) == expected
        except SyntaxError:
            return False
    return text.casefold() == case.expected


def preview(engine, model_ids=None, budget=None):
    s = engine.settings
    budget = s.calibration.budget if budget is None else budget
    if not isinstance(budget, (int, float)) or not 0 <= budget <= s.budgets.default_request_budget:
        raise ValueError("Calibration budget must fit the request budget")
    requested = set(model_ids or [])
    if len(requested) > s.calibration.max_models:
        raise ValueError("Select at most " + str(s.calibration.max_models) + " models")
    available = [m for m in s.models if m.enabled and m.status in {"available", "degraded", "unknown"} and (not requested or m.id in requested)]
    if requested - {m.id for m in available}:
        raise ValueError("Requested model is unavailable")
    # Unselected paid catalogs never turn into dozens of benchmark calls.
    available.sort(key=lambda m: (not is_local(s.providers[m.provider]), m.id))
    selected, rejected, total = [], [], 0.0
    cases = CASES[:s.calibration.max_cases]
    resolver = RoleResolver(s, engine.store)
    for original in available[:s.calibration.max_models]:
        provider = s.providers[original.provider]
        model = resolver.priced(original)
        if not provider.enabled or not allowed_provider(s, provider, "calibration"):
            rejected.append({"model": model.id, "reason": "PRIVACY_OR_PROVIDER_POLICY"})
            continue
        if model.input_price is None or model.output_price is None:
            rejected.append({"model": model.id, "reason": "PRICE_UNKNOWN"})
            continue
        # Include schema/tool overhead in the quote, not only the question length.
        maximum = sum(estimate_cost(model, token_upper_bound(messages(case)) + 1024, 256, reserve=True) for case in cases)
        if total + maximum > budget + 1e-10:
            rejected.append({"model": model.id, "reason": "BUDGET_EXCEEDED", "estimated_maximum": maximum})
            continue
        total += maximum
        selected.append({"model": model.id, "maximum": maximum, "local": is_local(provider), "cases": [case.name for case in cases]})
    quote = {"id": uid(), "fingerprint": s.fingerprint(), "budget": budget, "models": selected, "rejected": rejected,
             "estimated_maximum": total, "paid_approval_required": any(row["maximum"] > 0 for row in selected), "status": "preview", "created_at": time.time()}
    engine.store.metadata("calibration-quote:" + quote["id"], quote)
    return quote


async def run(engine, quote_id, allow_paid=False):
    key = "calibration-quote:" + quote_id
    with engine.store.transaction():
        quote = engine.store.metadata(key)
        if not quote or quote["status"] != "preview" or time.time()-quote["created_at"] > 900:
            raise ValueError("Calibration quote missing, expired or already used")
        if quote["fingerprint"] != engine.settings.fingerprint():
            raise ValueError("Configuration changed; preview calibration again")
        if quote["paid_approval_required"] and not allow_paid:
            raise ValueError("Review estimated maximum and explicitly allow paid calibration")
        quote["status"] = "running"
        engine.store.metadata(key, quote)
    results = []
    engine.store.economics("begin", quote_id, "calibration", "calibration")
    try:
        for selected in quote["models"]:
            model = next(m for m in engine.settings.models if m.id == selected["model"])
            observations = {}
            for case in CASES:
                if case.name not in selected["cases"]:
                    continue
                started = time.monotonic()
                passed, error = False, None
                try:
                    probe = "tools" if case.expected == "tool_echo" else None
                    result = await engine.providers.generate(model.model_copy(update={"status": "available"}), messages(case), "calibration", quote_id, "calibration", quote["budget"], max_output=256, probe=probe)
                    parsed = {} if probe else json.loads(result.text)
                    answer = parsed.get("answer") if isinstance(parsed, dict) and set(parsed) == {"answer"} else None
                    passed = objective_check(case, answer, result.raw)
                except (ProviderError, ValueError, RuntimeError) as exc:
                    error = getattr(exc, "code", type(exc).__name__)
                latency = time.monotonic()-started
                from .dna import observe
                observe(engine.store, model.id, case.dimension, passed, source="calibration")
                observations.setdefault(case.dimension, []).append(passed)
                row = {"model": model.id, "case": case.name, "passed": passed, "latency": latency, "error": error, "check": "objective_fixture"}
                results.append(row)
                engine.store.db.execute("INSERT OR REPLACE INTO benchmarks VALUES(?,?,?,?,?,?)", (model.id, case.name, time.time(), int(passed), json.dumps({"check": "objective_fixture", "error": error}), latency))
            # Small samples are only weak evidence, shrunk toward a conservative
            # prior. Display sample counts and do not claim real-world guarantees.
            model.quality = {**model.quality, **{dimension: (sum(passes)+1.6)/(len(passes)+2) for dimension, passes in observations.items()}}
            model.benchmarked_at = time.time()
            coding = observations.get("coding", [])
            if coding and all(coding):
                if model.capability_evidence.get("code", {}).get("source") != "user_override":
                    model.supports_code = True
                    model.capability_evidence["code"] = {"source": "active_probe", "supported": True, "timestamp": time.time(), "check": "bounded Python fixtures"}
            if all(observations.get("json_adherence", [False])):
                model.supports_text = True
            if any(row["passed"] for row in results if row["model"] == model.id):
                model.status = "available"
                protocol = engine.providers.protocol(engine.settings.providers[model.provider])
                capability = {"openai_chat": "chat_completions", "openai_responses": "responses_api"}.get(protocol)
                if capability and model.capability_evidence.get(capability, {}).get("source") != "user_override":
                    setattr(model, "supports_" + capability, True)
                    model.capability_evidence[capability] = {"source": "active_probe", "supported": True, "timestamp": time.time()}
            if observations.get("tool_use") == [True]:
                if model.capability_evidence.get("tools", {}).get("source") != "user_override":
                    model.supports_tools = True
                    model.capability_evidence["tools"] = {"source": "active_probe", "supported": True, "timestamp": time.time()}
            engine.store.save_model(model)
        quote["status"] = "complete"
    except BaseException:
        quote["status"] = "interrupted"
        raise
    finally:
        engine.store.metadata(key, quote)
        if quote["status"] == "complete":
            engine.store.economics("finish", quote_id)
        else:
            engine.store.economics("pause", quote_id, "interrupted")
    return {"quote_id": quote_id, "results": results, "costs": engine.store.costs(quote_id),
            "limitations": "Tiny objective fixtures estimate role suitability. They do not certify architecture quality, language coverage or real repository success."}


async def probe_model(manager, model, capability, budget=0, approved=False):
    allowed = {"text", "structured_output", "tools", "streaming", "vision", "reasoning", "responses_api", "chat_completions", "embeddings"}
    if capability not in allowed:
        raise ValueError("Unknown capability probe")
    provider = manager.settings.providers[model.provider]
    if not is_local(provider) and not approved:
        raise ValueError("Cloud probes require explicit approval of the budget")
    if budget < 0 or budget > manager.settings.budgets.default_request_budget:
        raise ValueError("Invalid probe budget")
    from .store import cache_key
    key = "probe:" + cache_key([model.id, model.deployment_name, model.protocols, capability, provider.model_dump(mode="json")])
    cached = manager.store.metadata(key)
    if cached and time.time()-cached["timestamp"] < 86400:
        return cached
    prompt = [{"role": "user", "content": 'Return only JSON {"answer":"ok"}.'}]
    schema = {"type": "object", "properties": {"answer": {"type": "string", "enum": ["ok"]}}, "required": ["answer"], "additionalProperties": False}
    candidate = model.model_copy(update={"supports_" + capability: True, "status": "available"})
    result = await manager.generate(candidate, prompt, "calibration", uid(), "probe", budget, max_output=64,
                                    schema=schema if capability == "structured_output" else None, probe=capability)
    evidence = result.raw.get("probe_evidence", {})
    if capability in {"tools", "streaming", "vision", "reasoning", "embeddings"}:
        supported = bool(evidence.get(capability))
    else:
        try:
            supported = json.loads(result.text) == {"answer": "ok"}
        except ValueError:
            supported = False
    record = {"source": "active_probe", "supported": supported, "timestamp": time.time(), "capability": capability}
    explicit = model.capability_evidence.get(capability, {}).get("source") == "user_override"
    if not explicit:
        setattr(model, "supports_" + capability, supported)
        if supported:
            model.status = "available"
    if not explicit:
        model.capability_evidence[capability] = record
    manager.store.metadata(key, record)
    manager.store.save_model(model)
    return record
