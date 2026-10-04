import ast, json, time
from .schema import uid
from .providers import parse_json, estimate_cost

CASES = [
    ("simple_edit", "Return Python function add(a,b) that returns their sum.", "python"),
    ("unit_test", "Return Python unittest code testing that sorted([3,1]) equals [1,3].", "python"),
    ("bug_diagnosis", "IndexError on empty list xs[0]. Return a safe Python first(xs) returning None if empty.", "python"),
    ("kotlin_android", "Describe StateFlow lifecycle collection in Android Compose in one sentence.", "text"),
    ("java", "Return a Java method int add(int a,int b).", "text"),
    ("python", "Return Python function square(n).", "python"),
    ("api_design", "Propose an idempotent HTTP API for creating a payment, in one sentence.", "text"),
    ("refactor", "Return a Python function is_even(n) with no if statement.", "python"),
    ("concurrency", "Describe why incrementing a shared counter needs synchronization.", "text"),
    ("architecture", "Describe a bounded job queue with backpressure in one sentence.", "text"),
    ("code_review", "Find the bug: def mean(xs): return sum(xs)/len(xs). Mention empty input.", "text"),
    ("documentation", "Document Python list.append in one sentence.", "text"),
    ("structured_json", "Return answer exactly equal to ok.", "exact"),
    ("tool_calling", "Describe the arguments for calling read_file(path='a.py').", "text")]

async def evaluate(engine, budget=0, model_id=None, calibrate=False):
    if budget < 0:
        raise ValueError("Budget cannot be negative")
    if budget == 0:
        return {"mode": "offline", "cases": len(CASES), "paid_calls": 0,
                "categories": [c[0] for c in CASES], "validation": "Run pytest for deterministic protocol, routing, security and mock DAG checks.",
                "calibration_updated": False}
    models = [m for m in engine.settings.models if m.enabled and (not model_id or m.id == model_id)]
    report, rid = [], uid()
    engine.store.economics("begin", rid, "eval", "evaluation")
    for model in models:
        for family, prompt, kind in CASES:
            if engine.store.costs(rid)["estimated_usd"] + estimate_cost(model, 2000, 512) > budget:
                break
            start = time.monotonic()
            passed, check = False, "schema_only"
            try:
                result = await engine.providers.generate(model, [{"role": "system", "content": 'Return JSON {"answer": "..."} only.'}, {"role": "user", "content": prompt}], "executor", rid, "eval", min(budget, engine.settings.budgets.default_request_budget), max_output=512, task_id=rid+"-"+model.id+"-"+family)
                answer = parse_json(result.text)["answer"]
                passed = isinstance(answer, str) and bool(answer.strip())
                if kind == "python":
                    ast.parse(answer); check = "python_syntax_only"
                if kind == "exact":
                    passed = answer == "ok"; check = "exact_match"
                if family == "code_review":
                    passed = "empty" in answer.lower(); check = "expected_finding"
            except Exception:
                passed = False
            latency = time.monotonic()-start
            report.append({"model": model.id, "family": family, "passed": passed, "check": check, "latency": latency})
            # Schema/syntax checks are not evidence of coding quality. Only objective result checks update priors.
            if calibrate and check in {"exact_match", "expected_finding"}:
                engine.store.success(model.id, family, passed, latency)
    engine.store.put("evaluation:"+rid, report)
    engine.store.economics("finish", rid)
    return {"request_id": rid, "results": report, "costs": engine.store.costs(rid), "limitations": "Java/Kotlin compile and semantic design grading require configured external harnesses. Tool protocol behavior is covered by integration tests; this prompt checks schema only."}
