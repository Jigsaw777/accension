"""First-class developer commands over the existing engine and management services."""
import json
from pathlib import Path
from .schema import Request, EgressBudget
from .contracts import contract_scope

UNHANDLED = object()


async def dispatch(args, engine, manager):
    command = args.command
    if command == "savings":
        if args.run:
            from .receipts import get
            receipt = get(engine, args.run)
            if args.reprice:
                return {"original": receipt.get("economics"), "recalculated_using_current_prices": engine.store.economics("calculate", receipt["request_id"], reprice=True)}
            return receipt.get("economics")
        if args.reprice:
            raise ValueError("Use --run RUN_ID with --reprice; historical receipts are preserved")
        return engine.store.economics("summary", "session" if args.session else args.period or "today", args.session) or {"available": False, "reason": "Savings accounting unavailable"}
    if command == "init":
        from .cli_setup import initialize
        result = initialize(manager, args)
        if result["provider"]:
            result["discovery"] = await engine.discovery.refresh(True, result["provider"]["provider"])
        return result
    if command == "provider":
        if args.action == "list":
            return manager.providers()
        if args.action == "add":
            from .cli_setup import provider_add
            result = provider_add(manager, args)
            result["discovery"] = await engine.discovery.refresh(True, result["provider"])
            return result
        if not args.name:
            raise ValueError("Provider instance ID required")
        if args.action == "test":
            from .conformance import check_provider
            if args.name not in engine.settings.providers:
                return {"plugin": engine.providers.plugins.get(args.name).manifest().model_dump(),
                        "status": "not_connected", "next": "accs provider add " + args.name}
            return await check_provider(engine, args.name, args.inference, args.budget, args.allow_paid)
        if args.action == "refresh":
            return await engine.discovery.refresh(True, args.name)
        return manager.remove_provider(args.name)
    if command in {"model", "models"}:
        action = getattr(args, "action", "list")
        if action in {"list", "search"}:
            return engine.store.models_page(getattr(args, "provider", None), args.name if action == "search" else getattr(args, "search", None),
                                            getattr(args, "offset", 0), getattr(args, "limit", 50))
        if action == "refresh":
            return await engine.discovery.refresh(True, args.provider)
        model = next((m for m in engine.settings.models if m.id == args.name), None)
        if model is None:
            raise ValueError("Unknown model ID; use accs model search TEXT")
        if action == "info":
            return model.profile()
        if action == "dna":
            from .dna import profile, reset
            return reset(engine.store, model.id) if args.reset else profile(engine.store, model)
        if action in {"enable", "disable"}:
            return manager.update_model(model.id, {"enabled": action == "enable"})
        from .calibration import probe_model
        return await probe_model(engine.providers, model, args.capability, args.budget, args.allow_paid)
    if command == "role":
        if args.action == "list":
            return engine.router.roles.all()
        if args.name not in engine.settings.roles:
            raise ValueError("Unknown role; use accs role list")
        if args.action == "explain":
            return engine.router.roles.resolve(args.name)
        previous = engine.settings.roles[args.name].model_dump()
        if args.action in {"auto", "reset"}:
            if args.action == "reset":
                from .config import RolePolicy
                previous = RolePolicy(locality="local-only" if args.name in {"classifier", "arbiter"} else "local-preferred").model_dump()
            return manager.set_role(args.name, {**previous, "strategy": "auto", "model": None})
        model = args.model_id or args.model
        if model and not any(m.id == model for m in engine.settings.models):
            raise ValueError("Unknown model ID")
        return manager.set_role(args.name, {**previous, "strategy": args.strategy or ("pinned" if model else "auto"), "model": model,
            "locality": args.locality or previous["locality"], "minimum_quality": args.minimum_quality if args.minimum_quality is not None else previous["minimum_quality"]})
    if command == "plugin":
        registry = engine.providers.plugins
        if args.action == "list":
            return {"loaded": registry.manifests(), "installed_untrusted": sorted(set(registry.available) - set(registry.plugins)), "errors": registry.errors,
                    "note": "Plugins are executable local Python code; installation and trust are explicit."}
        return registry.get(args.name).manifest().model_dump()
    if command in {"route", "run", "plan"}:
        if command == "plan" and args.task == "diff":
            from .axir import read, diff
            if len(args.files) != 2:
                raise ValueError("Use accs plan diff OLD.axir.json NEW.axir.json")
            return diff(read(args.files[0]), read(args.files[1]))
        if getattr(args, "files", []):
            raise ValueError("Quote the task as one argument")
        privacy = "LOCAL_ONLY" if args.local_only else args.privacy
        if command == "route" or args.dry_run:
            with contract_scope(mode=privacy, quality=args.quality, budget=EgressBudget(max_cloud_context_tokens_per_request=args.max_cloud_context)):
                return await engine.route(args.task, repo_path=args.repo)
        request = Request(task=args.task, repo_path=args.repo or str(Path.cwd()), constraints=args.constraint, budget=args.budget,
            session_id=args.session, privacy=privacy, max_cloud_context=args.max_cloud_context, quality=args.quality,
            execution_mode=args.mode)
        if command == "run":
            return await engine.run(request)
        plan = await engine.plan(request)
        result = plan.model_dump(mode="json")
        if args.export:
            from .axir import export
            export(engine, plan.plan_id, args.export)
            result["exported"] = str(Path(args.export).resolve())
        return result
    if command == "execute":
        plan_id = args.plan_id
        if Path(plan_id).is_file():
            from .axir import read, bind
            plan_id = bind(engine, read(plan_id), args.repo or str(Path.cwd())).plan_id
        return await engine.execute_plan(plan_id, args.repo or str(Path.cwd()))
    if command in {"inspect", "reroute"}:
        from .axir import read, reroute
        ir = read(args.file)
        return ir.model_dump(mode="json") if command == "inspect" else reroute(engine, ir)
    if command in {"resume", "rollback"}:
        from .recovery import recover
        from .task_api import run_status
        run = run_status(engine, args.run_id)
        return await recover(engine, run["run_id"], command)
    if command == "receipt":
        from .receipts import get, markdown
        value = get(engine, args.run_id)
        rendered = markdown(value) if args.markdown else json.dumps(value, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        return markdown(value) if args.markdown else value
    if command == "trace":
        from .task_api import run_status
        try:
            request = run_status(engine, args.request_id)["request_id"]
        except ValueError:
            request = args.request_id
        return engine.store.traces(request)
    if command == "lab":
        from .lab import compare
        return compare(engine, args.run_id)
    if command == "mode":
        if args.value:
            manager.set_policy({"control_plane": {"mode": args.value}})
        return {"mode": engine.settings.control_plane.mode,
                "native_execution": "accs run / MCP / task API", "gateway": "Individual inference routing; host retains its tools and filesystem."}
    if command == "calibrate":
        from .calibration import preview, run
        if args.local_only:
            engine.settings.control_plane.fully_local = True
        if args.quick:
            engine.settings.calibration.max_cases = 4
        if args.action == "run" and args.quote_id:
            return await run(engine, args.quote_id, args.allow_paid)
        quote = preview(engine, args.model, args.budget)
        if args.action == "run" or args.quick or args.local_only:
            # --allow-paid is the explicit opt-in; otherwise return the priced preview.
            if quote["paid_approval_required"] and not args.allow_paid:
                return {**quote, "next": "Review the estimate; rerun with --allow-paid or --local-only."}
            return await run(engine, quote["id"], args.allow_paid)
        return quote
    if command == "integrate":
        from .integrations import preview, install, undo, list_integrations
        if args.client == "list":
            return list_integrations(engine)
        if args.client == "undo":
            if not args.target:
                raise ValueError("Use accs integrate undo CLIENT")
            return undo(engine, args.target)
        quote = preview(engine, args.client, args.mode)
        if args.apply:
            return {"preview": quote, "result": install(engine, quote["id"])}
        return quote
    return UNHANDLED
