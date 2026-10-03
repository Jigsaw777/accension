from __future__ import annotations
import asyncio, difflib, json, time
from pathlib import Path
from .config import Settings
from .schema import Request, ExecutionPlan, TaskNode, WorkerResult, Review, uid
from .store import Store, BudgetExceeded, cache_key
from .providers import Providers, ProviderError, parse_json, estimate_cost, token_upper_bound
from .routing import Router, deterministic
from .context import ContextGraph, load_skills
from .safety import Edits, repo_lock, safe_path, validate, validation_argv, redact, SECRET

def conflicts(a, b):
    def paths(t):
        return {x.replace("\\", "/").casefold() for x in t.expected_artifacts + t.relevant_files}
    aw = {x.replace("\\", "/").casefold() for x in a.expected_artifacts}
    bw = {x.replace("\\", "/").casefold() for x in b.expected_artifacts}
    # A project test may read any file; serialize tasks with validation against writers.
    return bool(aw & paths(b) or bw & paths(a)) or not a.parallelizable or not b.parallelizable or bool(a.validation_commands or b.validation_commands)

class Engine:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.store = Store(settings)
        self.providers = Providers(settings, self.store, transport)
        self.router = Router(settings, self.store, self.providers)
        self.validation_lock = asyncio.Lock()
        from .discovery import DiscoveryService
        self.discovery = DiscoveryService(self)
        if not settings.mock:
            from .schema import Model
            from collections import deque
            ids = {m.id for m in settings.models}
            for item in self.store.metadata("discovered-models") or []:
                m = Model.model_validate(item)
                if m.id not in ids and m.provider in settings.providers:
                    settings.models.append(m)
                    self.providers.semaphores[m.id] = asyncio.Semaphore(m.concurrency_limit)
                    self.providers.windows[m.id] = deque()

    async def close(self):
        await self.router.laya.close()
        await self.providers.close()
        self.store.close()

    def prompt(self, role, schema=None):
        text = (self.settings.home / "prompts" / role / "v1.txt").read_text()
        return text + ("\nOutput JSON schema:\n" + json.dumps(schema.model_json_schema()) if schema else "")

    def prompt_version(self):
        return cache_key({str(p.relative_to(self.settings.home)): p.read_text() for p in sorted((self.settings.home/"prompts").rglob("*.txt"))})

    async def route(self, task, request_id=None):
        rid = request_id or uid()
        c = await self.router.classify(task, rid)
        candidates = self.router.candidates(c, "planner" if c.planning_required else "executor", ["code"])
        return {"request_id": rid, "classification": c.model_dump(), "candidates": [m.id for m in candidates], "jev_required": self.router.needs_jev(c),
                "status": "ready" if candidates else "unavailable", "note": "Routing preview; optional local Laya classification, no generation or file edits."}

    async def plan(self, request: Request, request_id=None, locked=False):
        rid = request_id or uid()
        if SECRET.search(request.task) or any(SECRET.search(c) for c in request.constraints):
            raise ValueError("Secret-like request text cannot be sent to a model")
        root = Path(request.repo_path).resolve()
        repo = self.settings.repository(str(root))
        if not locked:
            with repo_lock(root):
                return await self.plan(request, rid, locked=True)
        graph = ContextGraph(root, self.store, repo)
        graph.build()
        budget = min(request.budget if request.budget is not None else self.settings.budgets.default_request_budget, self.settings.budgets.default_request_budget)
        c = await self.router.classify(request.task, rid)
        key = cache_key("plan-v1", request.task, request.constraints, graph.fingerprint, self.settings.fingerprint(), self.prompt_version(), budget)
        cached = self.store.get(key)
        if cached:
            plan = ExecutionPlan.model_validate(cached)
            plan.request_id = rid
            plan.plan_id = uid()
            self.store.trace(rid, "plan_cache", hit=True)
        else:
            candidates = self.router.candidates(c, "planner", ["code"], repo.allow_cloud)
            if not candidates:
                raise ProviderError("No eligible planner meets capabilities, quality SLO, known prices and repository cloud policy")
            arbiter = await self.router.arbitrate(c, rid, request.session_id, budget, candidates)
            if arbiter and arbiter.model_id:
                candidates.sort(key=lambda m: m.id != arbiter.model_id)
            # Trivial explicitly named file changes can skip model planning.
            mentioned = re_files(request.task, graph.files)
            if not c.planning_required and mentioned:
                plan = ExecutionPlan(goal=request.task, constraints=request.constraints, success_criteria=[request.task], allowed_budget=budget,
                    tasks=[TaskNode(id="edit", title="Bounded edit", objective=request.task, relevant_files=mentioned, expected_artifacts=mentioned,
                                    acceptance_criteria=[request.task], validation_commands=list(repo.validation), task_type=c.task_family)], global_validation=list(repo.validation))
            else:
                manifest = {n: {"symbols": [s["name"] for s in d["symbols"]], "sha256": d["sha256"]} for n,d in graph.files.items()}
                payload = {"goal": request.task, "constraints": request.constraints, "repository": manifest, "registered_checks": list(repo.validation), "budget": budget}
                model = candidates[0]
                messages = [{"role": "system", "content": self.prompt("planner", ExecutionPlan)}, {"role": "user", "content": json.dumps(payload)}]
                result = await self.providers.generate(model, messages, "planner", rid, request.session_id, budget, schema=ExecutionPlan.model_json_schema())
                try:
                    plan = ExecutionPlan.model_validate(parse_json(result.text))
                except Exception:
                    self.store.health_result(model.id, False)
                    # One cheap formatting repair; never repeat a frontier planning call.
                    cheap = [m for m in candidates if m.tier < 4]
                    if not cheap:
                        raise ProviderError("Invalid planner JSON; no inexpensive repair model") from None
                    messages[-1]["content"] = json.dumps({"goal": request.task, "constraints": request.constraints, "invalid_plan": result.text, "instruction": "Repair JSON/schema only. Preserve goal and constraints."})
                    fixed = await self.providers.generate(cheap[0], messages, "repair", rid, request.session_id, budget)
                    plan = ExecutionPlan.model_validate(parse_json(fixed.text))
                plan.planner_model = model.id
                self.store.trace(rid, "planning", model=model.id, tasks=len(plan.tasks))
            plan.request_id = rid
            plan.allowed_budget = budget
            # Planner may add constraints; it cannot delete caller constraints.
            plan.constraints = list(dict.fromkeys(request.constraints + plan.constraints))
            plan.risk_level = max(plan.risk_level, c.risk)
            self.store.put(key, plan.model_dump(mode="json"))
        self.validate_plan(plan, root, repo)
        self.persist_plan(plan, root, graph.fingerprint, request.session_id)
        return plan

    def validate_plan(self, plan, root, repo):
        if not repo.validation:
            raise ValueError("Register at least one independent validation check before execution")
        for name, argv in repo.validation.items():
            validation_argv(repo.validation, name)
        for t in plan.tasks:
            for name in t.expected_artifacts + t.relevant_files:
                safe_path(root, name)
            for check in t.validation_commands:
                validation_argv(repo.validation, check)
            for skill in t.skills_required:
                if skill not in self.settings.skills:
                    raise ValueError("Planner requested an unregistered skill")
        for name in plan.global_validation:
            validation_argv(repo.validation, name)
        if SECRET.search(plan.model_dump_json()):
            raise ValueError("Secret-like text in plan")

    def persist_plan(self, plan, root, fingerprint, session):
        folder = safe_path(root, ".router/plans", internal=True)
        folder.mkdir(parents=True, exist_ok=True)
        safe_path(root, f".router/plans/{plan.plan_id}.json", internal=True).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        lines = [f"# Epic: {plan.goal}", "", plan.architecture_summary, "", "## Constraints", *[f"- {s}" for s in plan.constraints], "", "## Acceptance", *[f"- {s}" for s in plan.success_criteria]]
        for task in plan.tasks:
            lines += ["", f"## {task.jira_key or task.id}: {task.title}", task.objective, task.detailed_description,
                      f"Depends on: {', '.join(task.dependencies) or 'none'}", f"Files: {', '.join(task.expected_artifacts)}",
                      "Acceptance:", *[f"- {x}" for x in task.acceptance_criteria], "Checks: " + ", ".join(task.validation_commands)]
        safe_path(root, f".router/plans/{plan.plan_id}.md", internal=True).write_text("\n".join(lines), encoding="utf-8")
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?)", (plan.plan_id, str(root), fingerprint, "planned", json.dumps({"plan": plan.model_dump(mode="json"), "session": session})))

    async def run(self, request: Request):
        rid = uid()
        root = Path(request.repo_path).resolve()
        self.settings.repository(str(root))
        self.store.trace(rid, "ingress", task_hash=cache_key(request.task), repo_hash=cache_key(str(root)))
        with repo_lock(root):
            plan = await self.plan(request, rid, locked=True)
            if request.execution_mode == "plan-only":
                return {"request_id": rid, "plan_id": plan.plan_id, "status": "planned", "tasks": len(plan.tasks)}
            return await self._execute(plan.plan_id, root, request.session_id)

    async def execute_plan(self, plan_id, repo_path):
        root = Path(repo_path).resolve()
        self.settings.repository(str(root))
        with repo_lock(root):
            return await self._execute(plan_id, root)

    async def _execute(self, plan_id, root, session=None):
        row = self.store.db.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
        if not row or row["repo"] != str(root) or row["status"] != "planned":
            raise ValueError("Unknown, already executed, or non-resumable plan")
        data = json.loads(row["data"])
        plan = ExecutionPlan.model_validate(data["plan"])
        session = session or data["session"]
        repo = self.settings.repository(str(root))
        graph = ContextGraph(root, self.store, repo)
        graph.build()
        if graph.fingerprint != row["fingerprint"]:
            raise ValueError("Repository changed since planning; regenerate plan")
        self.validate_plan(plan, root, repo)
        self.store.db.execute("UPDATE runs SET status='running' WHERE id=?", (plan_id,))
        rid, completed, pending = plan.request_id, {}, {t.id: t for t in plan.tasks}
        edits = Edits(root, rid)
        try:
            while pending:
                ready = [t for t in pending.values() if set(t.dependencies) <= set(completed)]
                batch = []
                for task in ready:
                    if len(batch) >= self.settings.routing.max_parallel_workers:
                        break
                    if not any(conflicts(task, other) for other in batch):
                        batch.append(task)
                if not batch:
                    raise ValueError("DAG cannot make progress")
                self.store.trace(rid, "schedule", tasks=[t.id for t in batch], parallel_count=len(batch))
                jobs = [asyncio.create_task(self._worker(t, plan, graph, repo, completed, edits, session)) for t in batch]
                try:
                    results = await asyncio.gather(*jobs, return_exceptions=True)
                except BaseException:
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
                    raise
                failure = next((r for r in results if isinstance(r, BaseException)), None)
                if failure:
                    raise failure
                for t, result in zip(batch, results):
                    completed[t.id] = result
                    pending.pop(t.id)
                self.store.trace(rid, "integration", completed_tasks=len(completed))
            final = await validate(root, repo.validation, list(repo.validation), self.settings.routing.timeouts["tests"])
            if not all(x["passed"] for x in final):
                raise ValueError("Final independent validation failed")
            self.store.trace(rid, "final_validation", passed=True, checks=[x["check"] for x in final])
            result = {"request_id": rid, "plan_id": plan.plan_id, "status": "complete", "planner": plan.planner_model,
                      "tasks": len(completed), "files_changed": sorted(edits.written), "validation": [{k:v for k,v in x.items() if k != "output"} for x in final],
                      "models_used": sorted({r["model"] for r in completed.values()}), "escalations": sum(r["escalations"] for r in completed.values()),
                      "costs": self.store.costs(rid), "remaining_risks": [risk for r in completed.values() for risk in r["remaining_risks"]],
                      "plan_path": str(root / ".router" / "plans" / (plan.plan_id+".md"))}
            self.store.db.execute("UPDATE runs SET status='complete', data=? WHERE id=?", (json.dumps({**data, "result": result}), plan_id))
            return result
        except BaseException as exc:
            conflicts_left = edits.rollback()
            self.store.db.execute("UPDATE runs SET status='failed' WHERE id=?", (plan_id,))
            self.store.trace(rid, "failure", error_type=type(exc).__name__, rollback_conflicts=conflicts_left)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(f"Run {rid} failed ({type(exc).__name__}: {redact(str(exc))}). Router edits rolled back; conflicts: {conflicts_left}") from None

    async def _worker(self, task, plan, graph, repo, completed, edits, session):
        c = deterministic(task.objective)
        c.task_family = "critical" if task.risk >= self.settings.routing.jev_risk_threshold else task.task_type if task.task_type in self.settings.routing.quality_slos else "coding"
        c.risk = task.risk
        candidates = self.router.candidates(c, "executor", task.minimum_capabilities, repo.allow_cloud, max_tier=3)
        if not candidates:
            raise ProviderError(f"No eligible worker for task {task.id}")
        prior_error = ""
        model_index, escalations, spent = 0, 0, 0.0
        for attempt in range(min(task.max_attempts, self.settings.routing.max_attempts)):
            model = candidates[model_index]
            capsule, savings = graph.capsule(task, {d: completed[d] for d in task.dependencies}, plan.constraints,
                                            self.settings.routing.token_ceilings.get(c.task_family, 12000))
            if task.skills_required:
                capsule["skills"] = load_skills(self.settings, task.skills_required)
            if prior_error:
                capsule["repair_feedback"] = prior_error
            messages = [{"role": "system", "content": self.prompt("repair" if attempt else "executor", WorkerResult)}, {"role": "user", "content": json.dumps(capsule)}]
            estimate = estimate_cost(model, token_upper_bound(messages), model.max_output, reserve=True)
            if spent + estimate > task.max_cost:
                raise BudgetExceeded(f"Task {task.id} budget exhausted")
            self.store.trace(plan.request_id, "context", task=task.id, **savings)
            start = time.monotonic()
            charged = False
            try:
                result = await self.providers.generate(model, messages, "repair" if attempt else "executor", plan.request_id, session, plan.allowed_budget,
                                                       schema=WorkerResult.model_json_schema(), task_id=plan.request_id+"-"+task.id)
                spent += estimate_cost(model, result.usage.input_tokens, result.usage.output_tokens) if result.usage.input_tokens else estimate
                charged = True
                worker = WorkerResult.model_validate(parse_json(result.text))
                if worker.status != "complete" or worker.escalation_requested or not worker.files_changed:
                    raise ValueError("Worker requested escalation or returned no edits")
                edits.apply(worker.files_changed, task.expected_artifacts)
                async with self.validation_lock:
                    checks = await validate(graph.root, repo.validation, task.validation_commands, self.settings.routing.timeouts["tests"])
                if not all(x["passed"] for x in checks):
                    raise ValueError(json.dumps(checks))
                if task.risk >= self.settings.routing.reviewer_risk_threshold:
                    await self.review(task, plan, capsule, worker, model, session, repo)
                self.store.success(model.id, c.task_family, True, time.monotonic()-start)
                self.store.trace(plan.request_id, "worker", task=task.id, model=model.id, attempt=attempt+1, passed=True, checks=len(checks))
                return {"summary": worker.summary[:800], "files": [f.path for f in worker.files_changed], "model": model.id,
                        "tests": [x["check"] for x in checks], "escalations": escalations, "remaining_risks": worker.remaining_risks}
            except (ProviderError, ValueError) as exc:
                if not charged:
                    spent += estimate
                prior_error = str(redact(str(exc)))[:6000]
                self.store.success(model.id, c.task_family, False, time.monotonic()-start)
                self.store.trace(plan.request_id, "worker", task=task.id, model=model.id, attempt=attempt+1, passed=False, error_type=type(exc).__name__)
                if attempt >= 1 and attempt+1 < min(task.max_attempts, self.settings.routing.max_attempts):
                    if model_index+1 < len(candidates):
                        model_index += 1; escalations += 1
                    else:
                        stronger = self.router.candidates(c, "executor", task.minimum_capabilities, repo.allow_cloud, min_tier=model.tier+1, max_tier=min(4, model.tier+1))
                        if not stronger:
                            continue
                        decision = await self.router.arbitrate(c, plan.request_id, session, plan.allowed_budget, stronger, failures=attempt+1)
                        if stronger[0].tier == 4 and decision is None:
                            raise ProviderError("Frontier worker escalation requires Jev justification")
                        candidates += stronger
                        model_index += 1; escalations += 1
        raise ValueError(f"Task {task.id} exhausted bounded attempts: {prior_error}")

    async def review(self, task, plan, capsule, worker, implemented_by, session, repo):
        c = deterministic(task.objective)
        candidates = self.router.candidates(c, "reviewer", ["code"], repo.allow_cloud, max_tier=3)
        if not candidates:
            raise ProviderError("No eligible reviewer")
        candidates.sort(key=lambda m: m.provider == implemented_by.provider)
        originals = {f["path"]: f["content"] for f in capsule["files"]}
        diff = "\n".join("".join(difflib.unified_diff(originals[f.path].splitlines(True), f.content.splitlines(True), fromfile=f.path, tofile=f.path)) for f in worker.files_changed)
        payload = {"objective": task.objective, "acceptance": task.acceptance_criteria, "constraints": plan.constraints, "diff": diff}
        result = await self.providers.generate(candidates[0], [{"role": "system", "content": self.prompt("reviewer", Review)}, {"role": "user", "content": json.dumps(payload)}], "reviewer", plan.request_id, session, plan.allowed_budget)
        review = Review.model_validate(parse_json(result.text))
        self.store.trace(plan.request_id, "review", task=task.id, model=candidates[0].id, accepted=review.accepted)
        if not review.accepted:
            raise ValueError("Reviewer: " + "; ".join(review.findings))

def re_files(task, files):
    return [f for f in files if f in task][:4]
