from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from .cli_parser import parser
from .config import Repository, load
from .engine import Engine
from .schema import Request


def output(value, json_output=False):
    from .cli_output import human

    print(json.dumps(value, indent=2, default=str) if json_output else human(value))


async def dispatch(args, settings):
    engine = Engine(settings)
    try:
        from .management import Management

        management = Management(engine)
        if args.command in {"skill", "preset"}:
            from .skill_actions import cli_action

            return cli_action(args, engine)
        if args.command == "logs":
            from .observability import export_bundle, read_logs

            if args.action == "path":
                return {"path": str(engine.store.log.path)}
            if args.action == "export":
                return export_bundle(settings, args.output or args.value or "accension-diagnostics.zip")
            filters = {
                "level": args.level,
                "component": args.component,
                "run": args.run or args.value,
                "since": args.since,
                "limit": args.limit,
            }
            if args.follow:
                last = None
                while True:
                    rows = read_logs(settings, **filters)
                    for row in rows:
                        if last is None or row["timestamp"] > last:
                            output(row, args.json)
                    if rows:
                        last = rows[-1]["timestamp"]
                    await asyncio.sleep(1)
            return read_logs(settings, **filters)
        if args.command in {"run", "execute", "resume"}:
            from .cli_output import progress

            def observe(event):
                message = progress(event)
                if message:
                    print(message, file=sys.stderr, flush=True)

            engine.store.trace_listener = observe
        from .cli_commands import UNHANDLED
        from .cli_commands import dispatch as dispatch_new

        result = await dispatch_new(args, engine, management)
        if result is not UNHANDLED:
            return result
        if args.command == "route":
            return await engine.route(args.task, repo_path=args.repo)
        if args.command == "config":
            if args.action == "migrate":
                from .migration import migrate_files

                return migrate_files(settings.home)
            if args.action == "export":
                profile = management.export_profile()
                if args.file:
                    Path(args.file).write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
                    return {"exported": str(Path(args.file).resolve())}
                return profile
            if args.action == "import":
                if not args.file:
                    raise ValueError("Use --file for a declarative routing profile")
                return management.import_profile(json.loads(Path(args.file).read_text(encoding="utf-8")))
            return {
                "valid": True,
                "schema_version": 2,
                "models": len(settings.models),
                "providers": len(settings.providers),
            }
        if args.command == "provider":
            if args.action == "list":
                return management.providers()
            if not args.name:
                raise ValueError("Provider name required")
            if args.action == "test":
                from .conformance import check_provider

                return await check_provider(engine, args.name, args.inference, args.budget, args.allow_paid)
            if not args.kind:
                raise ValueError("Use --kind with a built-in or enabled provider plugin")
            fields = {}
            for item in args.field:
                key, value = item.split("=", 1)
                try:
                    fields[key] = json.loads(value)
                except ValueError:
                    fields[key] = value
            if args.endpoint:
                fields["endpoint"] = args.endpoint
            import getpass

            secret = getpass.getpass("Provider API key (stored in OS vault): ") if args.api_key else None
            return management.connect(args.name, args.kind, fields, secret)
        if args.command == "role":
            if args.action == "list":
                return engine.router.roles.all()
            if not args.name:
                raise ValueError("Role name required")
            return management.set_role(
                args.name,
                {
                    "strategy": args.strategy,
                    "model": args.model,
                    "locality": args.locality,
                    "minimum_quality": args.minimum_quality,
                },
            )
        if args.command == "profiles":
            return await management.action("profiles-reset", {})
        if args.command == "integration":
            from .integrations import install, preview

            quote = preview(engine, args.client)
            return install(engine, quote["id"]) if args.action == "install" else quote
        if args.command == "recovery":
            from .recovery import list_runs, recover

            if args.action == "list":
                return list_runs(engine)
            if not args.plan_id:
                raise ValueError("Plan ID required")
            return await recover(engine, args.plan_id, args.action)
        if args.command == "repo":
            if args.action == "list":
                return [r.model_dump() for r in settings.repositories]
            if not args.path:
                raise ValueError("Repository path required")
            commands = {name: json.loads(argv) for name, argv in (item.split("=", 1) for item in args.check)}
            return management.register_repository(
                args.path,
                args.privacy or ("CLOUD_ALLOWED" if args.allow_cloud else "LOCAL_ONLY"),
                commands,
                args.never_send,
            )
        if args.command == "discover":
            return await engine.discovery.refresh(force=True)
        if args.command in ("run", "plan"):
            request = Request(
                task=args.task,
                repo_path=args.repo,
                constraints=args.constraint,
                budget=args.budget,
                session_id=args.session,
            )
            if args.command == "plan":
                return (await engine.plan(request)).model_dump(mode="json")
            return await engine.run(request)
        if args.command == "execute":
            return await engine.execute_plan(args.plan_id, args.repo)
        if args.command == "doctor":
            from .doctor import doctor

            result = await doctor(engine)
            if args.bundle:
                from .observability import export_bundle

                result["bundle"] = export_bundle(settings, args.bundle)
            return result
        if args.command == "models":
            return [m.model_dump() for m in settings.models]
        if args.command == "trace":
            return engine.store.traces(args.request_id)
        if args.command == "costs":
            return engine.store.costs()
        if args.command == "cache":
            if args.action == "clear":
                engine.store.clear_cache()
            return engine.store.cache_stats()
        if args.command == "calibrate":
            from .calibration import preview, run

            if args.action == "run":
                if not args.quote_id:
                    raise ValueError("Use --quote-id from a calibration preview")
                return await run(engine, args.quote_id, args.allow_paid)
            return preview(engine, args.model, args.budget)
        if args.command == "eval":
            from .evaluation import evaluate

            return await evaluate(engine, args.budget, args.model, False)
    finally:
        await engine.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    from .observability import correlation
    from .schema import uid

    context_token = correlation.set({"request_id": uid()})
    import time

    started = time.monotonic()
    log = None
    code = 0
    try:
        if args.command == "version":
            from . import __version__

            output({"product": "Accension", "version": __version__, "aliases": ["accension", "router"]}, args.json)
            return
        if args.command == "completion":
            from .cli_parser import completion

            output(completion(args.shell), args.json)
            return
        settings = load(args.home, args.mock)
        from .observability import EventLog

        if args.debug:
            settings.log_level = "DEBUG"
        log = EventLog(settings)
        log.emit("cli", "start", command=args.command)
        if args.port:
            settings.port = args.port
        if args.host:
            settings.host = args.host
        if getattr(args, "remote", False) or getattr(args, "auth_token_env", None):
            raise ValueError(
                "Remote binding is not supported in this release. Use loopback with an authenticated SSH tunnel; no anonymous remote mode exists."
            )
        from .config import Settings

        settings = Settings.model_validate(settings.model_dump())
        if args.command == "azure-login":
            from .azure_auth import login

            output(login(settings), args.json)
        elif args.command in {"serve", "ui"}:
            import uvicorn

            from .app import create_app

            if args.command == "ui":
                from .launcher import existing_ui

                if existing_ui(settings, args.no_browser):
                    return
            app = create_app(settings)
            if args.command == "ui":
                from .launcher import launch

                launch(app, settings, args.no_browser)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=settings.host,
                    port=settings.port,
                    access_log=False,
                    log_level="warning",
                    proxy_headers=False,
                )
            )
            app.state.server = server
            (settings.state / "server.pid").write_text(str(os.getpid()))
            try:
                server.run()
            finally:
                (settings.state / "server.pid").unlink(missing_ok=True)
        elif args.command == "mcp":
            from .mcp_server import create_mcp

            create_mcp(settings).run(transport="stdio")
        elif args.command == "enroll":
            import uvicorn

            from .enroll import create_enrollment

            uvicorn.run(create_enrollment(settings), host="127.0.0.1", port=8766, access_log=False, log_level="warning")
        elif args.command in ("start", "restart", "status", "stop"):
            from . import service

            if args.command == "restart":
                service.stop(settings)
                value = service.start(settings)
            else:
                value = getattr(service, args.command)(settings)
            if args.command == "status":
                from .savings import SavingsEngine
                from .store import Store

                store = Store(settings)
                try:
                    store.savings = SavingsEngine(store)
                    value["savings_today"] = store.economics("summary")
                finally:
                    store.close()
            output(value, args.json)
            if args.command == "status" and not value["running"]:
                raise SystemExit(6)
        elif args.command == "launch":
            from .integrations import launch

            result = launch(settings, args.client, args.direct, args.arguments, args.dry_run)
            output(result, args.json)
            if result.get("exit_code"):
                raise SystemExit(result["exit_code"])
        elif args.command == "demo":
            path = Path(args.repo or settings.home / "examples/demo-repo").resolve()
            path.mkdir(parents=True, exist_ok=True)
            settings = load(args.home, mock=True)
            settings.repositories = [
                Repository(path=str(path), validation={"tests": ["{python}", "-m", "unittest", "discover", "-v"]})
            ]

            async def demo():
                engine = Engine(settings)
                try:
                    return await engine.run(
                        Request(
                            task="Add a greeting feature with named and blank input tests and usage documentation",
                            repo_path=str(path),
                        )
                    )
                finally:
                    await engine.close()

            output(asyncio.run(demo()), args.json)
        else:
            output(asyncio.run(dispatch(args, settings)), args.json)
    except KeyboardInterrupt:
        code = 130
        raise SystemExit(130) from None
    except SystemExit as exc:
        code = exc.code
        raise
    except Exception as exc:
        from pydantic import ValidationError

        from .cli_output import exit_code
        from .observability import redact

        message = (
            "; ".join(".".join(str(part) for part in error["loc"]) + ": " + error["type"] for error in exc.errors())
            if isinstance(exc, ValidationError)
            else redact(str(exc))
        )
        code = exit_code(exc)
        error_id = log.failure("cli", exc, command=args.command, exit_code=code) if log else None
        print(
            json.dumps({"error": message, "type": type(exc).__name__, "exit_code": code, "error_id": error_id})
            if args.json
            else f"Error: {message}\nError ID: {error_id or 'unavailable'}. Use accs {args.command} --help for accepted inputs.",
            file=sys.stderr,
        )
        if args.debug:
            import traceback

            print(
                json.dumps(
                    [
                        {"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                        for f in traceback.extract_tb(exc.__traceback__)
                    ]
                ),
                file=sys.stderr,
            )
        raise SystemExit(code) from None
    finally:
        if log:
            log.emit(
                "cli",
                "end",
                command=args.command,
                exit_code=code,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            log.close()
        correlation.reset(context_token)


if __name__ == "__main__":
    main()
