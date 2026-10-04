from __future__ import annotations

import asyncio
import difflib
import json
import time
from functools import wraps
from pathlib import Path

from .config import Settings
from .context import ContextGraph
from .contracts import contract_scope, strictest
from .privacy import repository_scope
from .providers import ProviderError, Providers, estimate_cost, parse_json, token_estimate, token_upper_bound
from .routing import Router, deterministic
from .safety import SECRET, Edits, redact, repo_lock, safe_path, validate, validation_argv
from .schema import EgressBudget, ExecutionPlan, Request, Review, TaskNode, WorkerResult, uid
from .store import BudgetExceeded, Store, cache_key


def active_operation(method):
    @wraps(method)
    async def guarded(engine, *args, **kwargs):
        engine.active_operations += 1
        try:
            return await method(engine, *args, **kwargs)
        finally:
            engine.active_operations -= 1

    return guarded


def conflicts(a, b):
    def paths(t):
        return {x.replace("\\", "/").casefold() for x in t.expected_artifacts + t.relevant_files}

    aw = {x.replace("\\", "/").casefold() for x in a.expected_artifacts}
    bw = {x.replace("\\", "/").casefold() for x in b.expected_artifacts}
    # A project test may read any file; serialize tasks with validation against writers.
    return (
        bool(aw & paths(b) or bw & paths(a))
        or not a.parallelizable
        or not b.parallelizable
        or bool(a.validation_commands or b.validation_commands)
    )


class Engine:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.configured_model_ids = {m.id for m in settings.models}
        self.store = Store(settings)
        from .skills import SkillRegistry

        self.skills = SkillRegistry(self.store)
        if not settings.mock:
            from .config import validate_endpoint
            from .schema import Provider

            for name, item in (self.store.metadata("detected-providers") or {}).items():
                if name not in settings.providers:
                    provider = Provider.model_validate(item)
                    validate_endpoint(provider, settings.port)
                    settings.providers[name] = provider
        self.providers = Providers(settings, self.store, transport)
        self.router = Router(settings, self.store, self.providers)
        self.validation_lock = asyncio.Lock()
        self.active_runs = set()
        self.active_operations = 0
        from .discovery import DiscoveryService

        self.discovery = DiscoveryService(self)
        if not settings.mock:
            from collections import deque

            from .schema import Model

            ids = {m.id for m in settings.models}
            configured_models = {m.id: m for m in settings.models}
            persisted = self.store.registry()
            known = {m.id for m in persisted}
            for item in self.store.metadata("discovered-models") or []:
                legacy = Model.model_validate(item)
                if legacy.id not in known:
                    persisted.append(legacy)
                    self.store.save_model(legacy)
            for m in persisted:
                configured = configured_models.get(m.id)
                if configured and configured.provider == m.provider and configured.deployment_name == m.deployment_name:
                    configured.quality = m.quality
                    configured.benchmarked_at = m.benchmarked_at
                    for capability, evidence in m.capability_evidence.items():
                        if (
                            configured.capability_evidence.get(capability, {}).get("source") != "user_override"
                            and evidence.get("source") == "active_probe"
                        ):
                            configured.capability_evidence[capability] = evidence
                            if hasattr(configured, "supports_" + capability):
                                setattr(configured, "supports_" + capability, evidence.get("supported", False))
                if m.id not in ids and m.provider in settings.providers:
                    settings.models.append(m)
                    ids.add(m.id)
                    self.providers.semaphores[m.id] = asyncio.Semaphore(m.concurrency_limit)
                    self.providers.windows[m.id] = deque()
        for model in settings.models:
            from .privacy import is_local

            model.locality = "local" if is_local(settings.providers[model.provider]) else "cloud"
            if not model.protocols:
                try:
                    model.protocols = [self.providers.protocol(settings.providers[model.provider])]
                except ProviderError:
                    pass  # An unavailable plugin must not prevent configuration mode.
            self.store.save_model(model)
        from .savings import SavingsEngine

        self.store.savings = SavingsEngine(self.store)
        from .recovery import interrupted

        interrupted(self)

    async def close(self):
        await self.router.classifier.close()
        await self.providers.close()
        self.store.economics("pause_owned")
        self.store.close()

    def prompt(self, role, schema=None):
        path = self.settings.home / "prompts" / role / "v1.txt"
        if not path.exists():
            path = Path(__file__).parent / "data" / "prompts" / role / "v1.txt"
        text = path.read_text(encoding="utf-8")
        return text + ("\nOutput JSON schema:\n" + json.dumps(schema.model_json_schema()) if schema else "")

    def prompt_version(self):
        folder = self.settings.home / "prompts"
        if not folder.exists():
            folder = Path(__file__).parent / "data" / "prompts"
        return cache_key(
            {str(p.relative_to(folder)): p.read_text(encoding="utf-8") for p in sorted(folder.rglob("*.txt"))}
        )

    @active_operation
    async def route(self, task, request_id=None, repo_path=None):
        rid = request_id or uid()
        graph, repo = None, None
        if repo_path:
            repo = self.settings.repository(repo_path)
            graph = ContextGraph(Path(repo.path), self.store, repo)
            await asyncio.to_thread(graph.build)
        c = self.router.decision_engine.decide(task, graph)
        allow_cloud = repo.cloud_allowed if repo else True
        candidates, selection = self.router.roles.select(
            c,
            "planner" if c.planning_required else "executor",
            c.required_capabilities,
            allow_cloud=allow_cloud,
            budget=self.settings.budgets.default_request_budget,
        )
        from .privacy import fully_local

        selected = next((item for item in selection["candidates"] if item["model"] == selection["selected"]), None)
        economics = self.store.economics("simulate", selection["selected"], 2000, 2000)
        return {
            "request_id": rid,
            "classification": c.model_dump(),
            "candidates": [m.id for m in candidates],
            "economics": economics,
            "selection": selection,
            "roles": self.router.roles.all(allow_cloud=allow_cloud),
            "arbiter_optional": self.router.needs_arbiter(c),
            "status": "ready" if candidates else "configuration_required",
            "privacy": "LOCAL_ONLY" if fully_local(self.settings) else repo.privacy_mode if repo else "CLOUD_ALLOWED",
            "cost_simulation": {
                "expected": selected["estimated_max_cost"] if selected else None,
                "best_case": 0 if self.settings.cache.enabled else None,
                "configured_maximum": self.settings.budgets.default_request_budget,
                "estimate_only": True,
            },
            "cloud_egress_estimate": {
                "input_tokens": 0 if selected and selected["local"] else 2000 if selected else None,
                "basis": "metadata-only candidate estimate; exact context is built during compilation",
                "source_files": None,
            },
            "inference_calls": 0,
            "note": "Local deterministic preview. No model calls or file edits.",
        }

    @active_operation
    async def plan(self, request: Request, request_id=None, locked=False):
        rid = request_id or uid()
        if SECRET.search(request.task) or any(SECRET.search(c) for c in request.constraints):
            raise ValueError("Secret-like request text cannot be sent to a model")
        repo = self.settings.repository(request.repo_path)
        root = Path(repo.path).resolve(strict=True)
        if not locked:
            with repository_scope(repo), repo_lock(root), self.request_scope(request, repo):
                try:
                    return await self.plan(request, rid, locked=True)
                except BaseException:
                    self.store.economics("finish", rid, "failed")
                    raise
        graph = ContextGraph(root, self.store, repo)
        self.store.economics("begin", rid, request.session_id)
        await asyncio.to_thread(graph.build)
        self.store.trace(rid, "graph_cache", **graph.cache_usage)
        budget = min(
            request.budget if request.budget is not None else self.settings.budgets.default_request_budget,
            self.settings.budgets.default_request_budget,
        )
        c = await self.router.classify(request.task, rid, graph)
        skill_contract = self.skills.resolve(request, c.task_family)
        key = cache_key(
            "plan-v3",
            request.model_dump(mode="json"),
            graph.fingerprint,
            self.settings.fingerprint(),
            self.prompt_version(),
            budget,
            skill_contract.model_dump(),
        )
        cached = self.store.get(key)
        if cached:
            plan = ExecutionPlan.model_validate(cached)
            source_request = plan.request_id
            plan.request_id = rid
            plan.plan_id = uid()
            self.store.trace(rid, "plan_cache", hit=True, source_request=source_request)
        else:
            candidates = self.router.candidates(c, "planner", ["code"], repo.cloud_allowed, budget=budget)
            arbiter = (
                await self.router.arbitrate(
                    c, rid, request.session_id, budget, candidates, allow_cloud=repo.cloud_allowed
                )
                if candidates
                else None
            )
            if arbiter and arbiter.model_id:
                candidates.sort(key=lambda m: m.id != arbiter.model_id)
            # Trivial explicitly named file changes can skip model planning.
            mentioned = re_files(request.task, graph.files)
            if not c.planning_required and mentioned:
                plan = ExecutionPlan(
                    goal=request.task,
                    constraints=request.constraints,
                    success_criteria=[request.task],
                    allowed_budget=budget,
                    tasks=[
                        TaskNode(
                            id="edit",
                            title="Bounded edit",
                            objective=request.task,
                            relevant_files=mentioned,
                            expected_artifacts=mentioned,
                            acceptance_criteria=[request.task],
                            validation_commands=list(repo.validation),
                            task_type=c.task_family,
                        )
                    ],
                    global_validation=list(repo.validation),
                )
            else:
                if not candidates:
                    raise ProviderError(
                        "No eligible planner meets capabilities, quality SLO, pricing and repository privacy policy. Connect or calibrate a model."
                    )
                manifest = {
                    n: {"symbols": [s["name"] for s in d["symbols"]], "sha256": d["sha256"]}
                    for n, d in graph.files.items()
                }
                payload = {
                    "goal": request.task,
                    "constraints": request.constraints,
                    "repository": manifest,
                    "registered_checks": list(repo.validation),
                    "budget": budget,
                }
                messages = [
                    {"role": "system", "content": self.prompt("planner", ExecutionPlan)},
                    {"role": "user", "content": json.dumps(payload)},
                ]
                candidates = self.router.candidates(
                    c, "planner", ["code"], repo.cloud_allowed, inputs=token_upper_bound(messages), budget=budget
                )
                if arbiter and arbiter.model_id:
                    candidates.sort(key=lambda m: m.id != arbiter.model_id)
                plan = None
                for model in candidates[: self.settings.routing.max_attempts]:
                    try:
                        with contract_scope(files=list(manifest)):
                            result = await self.providers.generate(
                                model,
                                messages,
                                "planner",
                                rid,
                                request.session_id,
                                budget,
                                schema=ExecutionPlan.model_json_schema(),
                            )
                        try:
                            plan = ExecutionPlan.model_validate(parse_json(result.text))
                        except ValueError:
                            self.store.outcome(model.id, "planning", schema_adherence=False)
                            repair = [
                                messages[0],
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "goal": request.task,
                                            "constraints": request.constraints,
                                            "invalid_plan": result.text,
                                            "instruction": "Repair JSON/schema only. Preserve goal and constraints.",
                                        }
                                    ),
                                },
                            ]
                            cheap = [
                                m
                                for m in self.router.candidates(
                                    c,
                                    "repair",
                                    ["code"],
                                    repo.cloud_allowed,
                                    inputs=token_upper_bound(repair),
                                    budget=budget,
                                )
                                if estimate_cost(m, 2000, 2000) <= estimate_cost(model, 2000, 2000)
                            ]
                            if not cheap:
                                raise ProviderError("No eligible inexpensive repair model") from None
                            fixed = await self.providers.generate(
                                cheap[0], repair, "repair", rid, request.session_id, budget
                            )
                            plan = ExecutionPlan.model_validate(parse_json(fixed.text))
                        break
                    except (ProviderError, ValueError) as exc:
                        self.store.trace(rid, "planner_fallback", model=model.id, error_type=type(exc).__name__)
                if plan is None:
                    raise ProviderError(
                        "All eligible planners failed or exceeded context; split the request or configure a larger-context model"
                    )
                plan.planner_model = model.id
                self.store.trace(rid, "planning", model=model.id, tasks=len(plan.tasks))
            plan.request_id = rid
            plan.allowed_budget = budget
            # Planner may add constraints; it cannot delete caller constraints.
            plan.constraints = list(dict.fromkeys(request.constraints + plan.constraints))
            plan.risk_level = max(plan.risk_level, c.risk)
            self.store.put(key, plan.model_dump(mode="json"))
        plan.privacy_contract = strictest(plan.privacy_contract, request.privacy, repo.privacy_mode)
        plan.egress_budget = EgressBudget(
            max_cloud_context_tokens_per_request=request.max_cloud_context,
            max_cloud_files_per_request=request.max_cloud_files,
        )
        plan.quality_contract = max(plan.quality_contract or 0, request.quality or 0) or None
        plan.task_type = c.task_family
        for task in plan.tasks:
            task.skills_required = (
                [
                    s.id
                    for s in self.skills.compose_ids(
                        [s.id for s in skill_contract.skills] + task.skills_required, skill_contract.token_budget
                    )
                ]
                if request.skill_mode != "off"
                else []
            )
        plan.skill_contract = self.skills.freeze(
            [s for t in plan.tasks for s in t.skills_required],
            skill_contract.preset_id,
            skill_contract.suggestion_source,
            skill_contract.token_budget,
        )
        for task in plan.tasks:
            task.skills_required = [s.id for s in plan.skill_contract.skills if s.id in task.skills_required]
        self.validate_plan(plan, root, repo)
        self.persist_plan(plan, root, graph.fingerprint, request.session_id, graph.portable_fingerprint)
        self.store.economics("pause", rid)
        return plan

    def request_scope(self, request, repo):
        return contract_scope(
            mode=strictest(request.privacy, repo.privacy_mode),
            quality=request.quality,
            budget=EgressBudget(
                max_cloud_context_tokens_per_request=request.max_cloud_context,
                max_cloud_files_per_request=request.max_cloud_files,
            ),
            repository=cache_key(str(Path(repo.path).resolve())),
        )

    def validate_plan(self, plan, root, repo):
        self.skills.load([s.id for s in plan.skill_contract.skills], plan.skill_contract)
        if not repo.validation:
            raise ValueError("Register at least one independent validation check before execution")
        for name in repo.validation:
            validation_argv(repo.validation, name)
        for t in plan.tasks:
            for name in t.expected_artifacts + t.relevant_files:
                safe_path(root, name)
            for check in t.validation_commands:
                validation_argv(repo.validation, check)
            self.skills.load(t.skills_required, plan.skill_contract)
            if t.tools_required and any(
                tool not in self.settings.tool_registry and tool != "propose_file_changes" for tool in t.tools_required
            ):
                raise ValueError("Planner requested an unregistered tool")
        for name in plan.global_validation:
            validation_argv(repo.validation, name)
        if SECRET.search(plan.model_dump_json()):
            raise ValueError("Secret-like text in plan")
        # Dependency summaries and shared edit artifacts inherit stricter privacy.
        nodes = {t.id: t for t in plan.tasks}
        changed = True
        while changed:
            changed = False
            local_files = {
                f
                for t in plan.tasks
                if not t.cloud_eligible or t.privacy_class == "LOCAL_ONLY"
                for f in t.expected_artifacts + t.relevant_files
            }
            for t in plan.tasks:
                mode = strictest(
                    plan.privacy_contract,
                    t.privacy_class,
                    "LOCAL_ONLY"
                    if not t.cloud_eligible or local_files.intersection(t.relevant_files + t.expected_artifacts)
                    else None,
                    *(nodes[d].privacy_class for d in t.dependencies),
                )
                if t.privacy_class != mode:
                    t.privacy_class = mode
                    changed = True

    def persist_plan(self, plan, root, fingerprint, session, portable_fingerprint=None):
        folder = safe_path(root, ".router/plans", internal=True)
        folder.mkdir(parents=True, exist_ok=True)
        safe_path(root, f".router/plans/{plan.plan_id}.json", internal=True).write_text(
            plan.model_dump_json(indent=2), encoding="utf-8"
        )
        lines = [
            f"# Epic: {plan.goal}",
            "",
            plan.architecture_summary,
            "",
            "## Constraints",
            *[f"- {s}" for s in plan.constraints],
            "",
            "## Acceptance",
            *[f"- {s}" for s in plan.success_criteria],
        ]
        for task in plan.tasks:
            lines += [
                "",
                f"## {task.jira_key or task.id}: {task.title}",
                task.objective,
                task.detailed_description,
                f"Depends on: {', '.join(task.dependencies) or 'none'}",
                f"Files: {', '.join(task.expected_artifacts)}",
                "Acceptance:",
                *[f"- {x}" for x in task.acceptance_criteria],
                "Checks: " + ", ".join(task.validation_commands),
            ]
        safe_path(root, f".router/plans/{plan.plan_id}.md", internal=True).write_text(
            "\n".join(lines), encoding="utf-8"
        )
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                (
                    plan.plan_id,
                    str(root),
                    fingerprint,
                    "planned",
                    json.dumps(
                        {
                            "plan": plan.model_dump(mode="json"),
                            "session": session,
                            "portable_fingerprint": portable_fingerprint,
                        }
                    ),
                ),
            )

    @active_operation
    async def run(self, request: Request):
        from .observability import correlation

        rid = (correlation.get() or {}).get("request_id") or uid()
        repo = self.settings.repository(request.repo_path)
        root = Path(repo.path).resolve(strict=True)
        self.store.trace(rid, "ingress", task_hash=cache_key(request.task), repo_hash=cache_key(str(root)))
        with repository_scope(repo), repo_lock(root), self.request_scope(request, repo):
            try:
                plan = await self.plan(request, rid, locked=True)
            except BaseException:
                self.store.economics("finish", rid, "failed")
                raise
            if request.execution_mode == "plan-only":
                return {"request_id": rid, "plan_id": plan.plan_id, "status": "planned", "tasks": len(plan.tasks)}
            return await self._execute(plan.plan_id, root, request.session_id)

    @active_operation
    async def execute_plan(self, plan_id, repo_path):
        repo = self.settings.repository(repo_path)
        root = Path(repo.path).resolve(strict=True)
        with repository_scope(repo), repo_lock(root):
            return await self._execute(plan_id, root)

    @active_operation
    async def _execute(self, plan_id, root, session=None, resume=False, scoped=False):
        row = self.store.db.execute("SELECT * FROM runs WHERE id=?", (plan_id,)).fetchone()
        if not row or row["repo"] != str(root) or row["status"] != "planned":
            raise ValueError("Unknown, already executed, or non-resumable plan")
        data = json.loads(row["data"])
        plan = ExecutionPlan.model_validate(data["plan"])
        if not scoped:
            from .observability import scope

            with (
                scope(request_id=plan.request_id, run_id=plan.plan_id, plan_id=plan.plan_id),
                contract_scope(
                    mode=plan.privacy_contract,
                    budget=plan.egress_budget,
                    quality=plan.quality_contract,
                    repository=cache_key(str(root)),
                ),
            ):
                return await self._execute(plan_id, root, session, resume, scoped=True)
        session = session or data["session"]
        repo = self.settings.repository(str(root))
        graph = ContextGraph(root, self.store, repo)
        await asyncio.to_thread(graph.build)
        if graph.fingerprint != row["fingerprint"]:
            raise ValueError("Repository changed since planning; regenerate plan")
        self.validate_plan(plan, root, repo)
        self.store.db.execute("UPDATE runs SET status='running' WHERE id=?", (plan_id,))
        completed = data.get("completed", {}) if resume else {}
        rid, pending = plan.request_id, {t.id: t for t in plan.tasks if t.id not in completed}
        edits = Edits.restore(root, rid) if resume else Edits(root, rid)
        from . import receipts

        starting = data.get("starting") or {
            "fingerprint": graph.portable_fingerprint,
            "git": await asyncio.to_thread(receipts.git_state, root),
            "timestamp": time.time(),
        }
        data["starting"] = starting
        self.store.db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data), plan_id))
        self.active_runs.add(plan_id)
        self.store.economics("begin", rid, session, run=plan_id)
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
                jobs = [
                    asyncio.create_task(self._worker(t, plan, graph, repo, completed, edits, session)) for t in batch
                ]
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
                for t, result in zip(batch, results, strict=True):
                    completed[t.id] = result
                    pending.pop(t.id)
                await asyncio.to_thread(graph.build)
                data = {**data, "completed": completed}
                self.store.db.execute(
                    "UPDATE runs SET fingerprint=?, data=? WHERE id=?", (graph.fingerprint, json.dumps(data), plan_id)
                )
                self.store.trace(rid, "integration", completed_tasks=len(completed))
            final = await validate(
                root, repo.validation, list(repo.validation), self.settings.routing.timeouts["tests"]
            )
            self.store.trace(
                rid, "validation", validation=[{k: v for k, v in x.items() if k != "output"} for x in final]
            )
            if not all(x["passed"] for x in final):
                raise ValueError("Final independent validation failed")
            self.store.trace(rid, "final_validation", passed=True, checks=[x["check"] for x in final])
            result = {
                "request_id": rid,
                "plan_id": plan.plan_id,
                "status": "complete",
                "planner": plan.planner_model,
                "tasks": len(completed),
                "files_changed": sorted(edits.written),
                "validation": [{k: v for k, v in x.items() if k != "output"} for x in final],
                "models_used": sorted({r["model"] for r in completed.values()}),
                "escalations": sum(r["escalations"] for r in completed.values()),
                "costs": self.store.costs(rid),
                "remaining_risks": [risk for r in completed.values() for risk in r["remaining_risks"]],
                "plan_path": str(root / ".router" / "plans" / (plan.plan_id + ".md")),
            }
            self.store.db.execute(
                "UPDATE runs SET status='complete', data=? WHERE id=?",
                (json.dumps({**data, "result": result}), plan_id),
            )
            return result
        except BaseException as exc:
            conflicts_left = edits.rollback()
            self.store.db.execute("UPDATE runs SET status='failed' WHERE id=?", (plan_id,))
            error_id = self.store.log.failure(
                "execution", exc, request_id=rid, run_id=plan.plan_id, plan_id=plan.plan_id
            )
            self.store.trace(
                rid,
                "failure",
                error_type=type(exc).__name__,
                error_id=error_id,
                run_id=plan.plan_id,
                rollback_conflicts=conflicts_left,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(
                f"Run {rid} failed ({type(exc).__name__}: {redact(str(exc))}). Error ID: {error_id}. Router edits rolled back; conflicts: {conflicts_left}"
            ) from None
        finally:
            status = self.store.db.execute("SELECT status FROM runs WHERE id=?", (plan_id,)).fetchone()[0]
            try:
                receipts.save(self, plan, root, edits, starting, status)
            finally:
                self.active_runs.discard(plan_id)

    async def _worker(self, task, plan, graph, repo, completed, edits, session):
        blocked = {
            f for t in plan.tasks if t.privacy_class == "LOCAL_ONLY" for f in t.expected_artifacts + t.relevant_files
        }
        with contract_scope(
            mode=task.privacy_class,
            node=task.id,
            node_tokens=task.max_cloud_context,
            node_files=task.max_cloud_files,
            quality=task.quality_slo,
            blocked_files=blocked if task.privacy_class != "LOCAL_ONLY" else [],
        ):
            return await self._worker_scoped(task, plan, graph, repo, completed, edits, session)

    async def _worker_scoped(self, task, plan, graph, repo, completed, edits, session):
        c = deterministic(task.objective)
        c.task_family = (
            "critical"
            if task.risk >= self.settings.routing.arbiter_risk_threshold
            else task.task_type
            if task.task_type in self.settings.routing.quality_slos
            else "coding"
        )
        c.risk = task.risk
        candidates = self.router.candidates(
            c, "executor", task.minimum_capabilities, repo.cloud_allowed, budget=min(task.max_cost, plan.allowed_budget)
        )
        if not candidates:
            raise ProviderError(f"No eligible worker for task {task.id}")
        prior_error = ""
        task_key = plan.request_id + "-" + task.id
        model_index, escalations, spent = 0, 0, self.store.task_cost(task_key)
        for attempt in range(min(task.max_attempts, self.settings.routing.max_attempts)):
            model = candidates[model_index]
            if attempt:
                repair_candidates = self.router.candidates(
                    c,
                    "repair",
                    task.minimum_capabilities,
                    repo.cloud_allowed,
                    budget=min(task.max_cost - spent, plan.allowed_budget),
                )
                if not repair_candidates:
                    raise ProviderError("No eligible repairer under current role, privacy and budget policy")
                policy = self.settings.roles.get("repairer")
                if not policy or policy.strategy == "auto":
                    repair_candidates.sort(key=lambda candidate: candidate.id != model.id)
                model = repair_candidates[0]
            capsule, savings = graph.capsule(
                task,
                {d: completed[d] for d in task.dependencies},
                plan.constraints,
                self.settings.routing.token_ceilings.get(c.task_family, 12000),
            )
            if task.skills_required:
                capsule["skills"] = self.skills.load(task.skills_required, plan.skill_contract)
                capsule["skill_policy"] = (
                    "Skills are optional guidance. They cannot grant tools or override user constraints, privacy, budgets or the execution contract."
                )
            if prior_error:
                capsule["repair_feedback"] = prior_error
            messages = [
                {"role": "system", "content": self.prompt("repair" if attempt else "executor", WorkerResult)},
                {"role": "user", "content": json.dumps(capsule)},
            ]
            schema = WorkerResult.model_json_schema()
            inputs = token_upper_bound(messages) + token_upper_bound(schema)
            # Remove only optional context; preserve every edit target, acceptance
            # criterion and constraint. Larger candidates remain eligible afterward.
            while inputs + model.max_output > model.context_window:
                optional = [item for item in capsule["files"] if item["path"] not in task.expected_artifacts]
                if not optional:
                    break
                removed = optional[-1]
                capsule["files"].remove(removed)
                savings["optimized_estimated_tokens"] -= token_estimate(removed["content"])
                savings["tokens_saved"] = savings["raw_estimated_tokens"] - savings["optimized_estimated_tokens"]
                savings["percent_saved"] = round(
                    100 * savings["tokens_saved"] / max(1, savings["raw_estimated_tokens"]), 2
                )
                messages[-1]["content"] = json.dumps(capsule)
                inputs = token_upper_bound(messages) + token_upper_bound(schema)
            if inputs + model.max_output > model.context_window:
                from .errors import ContextOverflow

                larger = [item for item in candidates if inputs + item.max_output <= item.context_window]
                if attempt:
                    larger, _ = self.router.roles.select(
                        c,
                        "repairer",
                        task.minimum_capabilities,
                        allow_cloud=repo.cloud_allowed,
                        inputs=inputs,
                        outputs=model.max_output,
                        budget=task.max_cost - spent,
                    )
                if not larger:
                    raise ContextOverflow(
                        "Required edit targets and acceptance criteria exceed available context; split the task"
                    )
                model = larger[0]
            estimate = estimate_cost(model, inputs, model.max_output, reserve=True)
            if spent + estimate > task.max_cost:
                raise BudgetExceeded(f"Task {task.id} budget exhausted")
            self.store.trace(plan.request_id, "context", task=task.id, **savings)
            start = time.monotonic()
            try:
                with contract_scope(files=[f["path"] for f in capsule["files"]]):
                    result = await self.providers.generate(
                        model,
                        messages,
                        "repair" if attempt else "executor",
                        plan.request_id,
                        session,
                        plan.allowed_budget,
                        schema=schema,
                        task_id=task_key,
                        call_budget=task.max_cost - spent,
                    )
                spent = self.store.task_cost(task_key)
                worker = WorkerResult.model_validate(parse_json(result.text))
                if worker.status != "complete" or worker.escalation_requested or not worker.files_changed:
                    raise ValueError("Worker requested escalation or returned no edits")
                edits.apply(worker.files_changed, task.expected_artifacts)
                async with self.validation_lock:
                    checks = await validate(
                        graph.root, repo.validation, task.validation_commands, self.settings.routing.timeouts["tests"]
                    )
                self.store.trace(
                    plan.request_id,
                    "validation",
                    task=task.id,
                    validation=[{k: v for k, v in x.items() if k != "output"} for x in checks],
                )
                if not all(x["passed"] for x in checks):
                    raise ValueError(json.dumps(checks))
                if task.risk >= self.settings.routing.reviewer_risk_threshold:
                    await self.review(
                        task, plan, capsule, worker, model, session, repo, remaining=task.max_cost - spent
                    )
                    spent = self.store.task_cost(task_key)
                self.store.success(model.id, c.task_family, True, time.monotonic() - start)
                if checks:
                    self.observe_task(model.id, c.task_family, True, graph, task)
                self.store.outcome(
                    model.id,
                    c.task_family,
                    tests_pass=True,
                    repair_required=attempt > 0,
                    escalation_required=escalations > 0,
                    schema_adherence=True,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cached_tokens=result.usage.cached_tokens,
                    estimated_cost=spent,
                )
                self.store.trace(
                    plan.request_id,
                    "worker",
                    task=task.id,
                    model=model.id,
                    attempt=attempt + 1,
                    passed=True,
                    checks=len(checks),
                )
                return {
                    "summary": worker.summary[:800],
                    "files": [f.path for f in worker.files_changed],
                    "model": model.id,
                    "tests": [x["check"] for x in checks],
                    "escalations": escalations,
                    "remaining_risks": worker.remaining_risks,
                }
            except (ProviderError, ValueError) as exc:
                spent = self.store.task_cost(task_key)
                prior_error = str(redact(str(exc)))[:6000]
                self.store.success(model.id, c.task_family, False, time.monotonic() - start)
                self.observe_task(model.id, c.task_family, False, graph, task)
                self.store.trace(
                    plan.request_id,
                    "worker",
                    task=task.id,
                    model=model.id,
                    attempt=attempt + 1,
                    passed=False,
                    error_type=type(exc).__name__,
                )
                if (attempt >= 1 or isinstance(exc, ProviderError)) and attempt + 1 < min(
                    task.max_attempts, self.settings.routing.max_attempts
                ):
                    if model_index + 1 < len(candidates):
                        model_index += 1
                        escalations += 1
                        self.store.trace(
                            plan.request_id,
                            "escalation",
                            task=task.id,
                            from_model=model.id,
                            to_model=candidates[model_index].id,
                            reason_codes=["VERIFICATION_OR_PROVIDER_FAILURE"],
                        )
        raise ValueError(f"Task {task.id} exhausted bounded attempts: {prior_error}")

    def observe_task(self, model, family, passed, graph, task):
        from .dna import observe

        observe(self.store, model, family, passed)
        observe(self.store, model, family, passed, specialization="repo:" + cache_key(str(graph.root))[:16])
        for extension in {Path(name).suffix for name in task.expected_artifacts} - {""}:
            observe(self.store, model, family, passed, specialization="language:" + extension)

    async def review(self, task, plan, capsule, worker, implemented_by, session, repo, remaining=None):
        c = deterministic(task.objective)
        candidates = self.router.candidates(
            c, "reviewer", ["code"], repo.cloud_allowed, excluded={implemented_by.id}, budget=remaining
        )
        if not candidates:
            self.store.trace(
                plan.request_id,
                "review",
                task=task.id,
                status="deterministic_validation",
                reason_code="NO_INDEPENDENT_REVIEWER",
            )
            return 0
        policy = self.settings.roles.get("reviewer")
        if not policy or policy.strategy == "auto":
            candidates.sort(key=lambda m: m.provider == implemented_by.provider)
        originals = {f["path"]: f["content"] for f in capsule["files"]}
        diff = "\n".join(
            "".join(
                difflib.unified_diff(
                    originals[f.path].splitlines(True), f.content.splitlines(True), fromfile=f.path, tofile=f.path
                )
            )
            for f in worker.files_changed
        )
        payload = {
            "objective": task.objective,
            "acceptance": task.acceptance_criteria,
            "constraints": plan.constraints,
            "diff": diff,
        }
        messages = [
            {"role": "system", "content": self.prompt("reviewer", Review)},
            {"role": "user", "content": json.dumps(payload)},
        ]
        with contract_scope(files=[f.path for f in worker.files_changed]):
            result = await self.providers.generate(
                candidates[0],
                messages,
                "reviewer",
                plan.request_id,
                session,
                plan.allowed_budget,
                call_budget=remaining,
                task_id=plan.request_id + "-" + task.id,
            )
        review = Review.model_validate(parse_json(result.text))
        self.store.trace(plan.request_id, "review", task=task.id, model=candidates[0].id, accepted=review.accepted)
        if not review.accepted:
            raise ValueError("Reviewer: " + "; ".join(review.findings))
        u = result.usage
        return (
            estimate_cost(candidates[0], u.input_tokens, u.output_tokens, u.cached_tokens, u.cache_write_tokens)
            if u.input_tokens or u.output_tokens
            else estimate_cost(candidates[0], token_upper_bound(messages), candidates[0].max_output, reserve=True)
        )


def re_files(task, files):
    return [f for f in files if f in task][:4]
