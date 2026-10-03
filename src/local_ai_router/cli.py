from __future__ import annotations
import argparse, asyncio, json, os, subprocess, sys
from pathlib import Path
import httpx, yaml
from .config import load, Repository
from .schema import Request
from .engine import Engine

def output(value):
    print(json.dumps(value, indent=2, default=str))

async def dispatch(args, settings):
    engine = Engine(settings)
    try:
        if args.command == "route":
            return await engine.route(args.task)
        if args.command == "discover":
            return await engine.discovery.refresh(force=True)
        if args.command in ("run", "plan"):
            request = Request(task=args.task, repo_path=args.repo, constraints=args.constraint, budget=args.budget, session_id=args.session)
            if args.command == "plan":
                return (await engine.plan(request)).model_dump(mode="json")
            return await engine.run(request)
        if args.command == "execute":
            return await engine.execute_plan(args.plan_id, args.repo)
        if args.command == "doctor":
            from .doctor import doctor
            return await doctor(engine)
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
        if args.command in ("eval", "calibrate"):
            from .evaluation import evaluate
            return await evaluate(engine, args.budget, args.model, args.command == "calibrate")
    finally:
        await engine.close()

def parser():
    p = argparse.ArgumentParser(prog="router", description="Local cost-aware gateway and task DAG executor")
    p.add_argument("--home", default=os.getenv("ROUTER_HOME"))
    p.add_argument("--mock", action="store_true", help="Use only explicit deterministic fixture models")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve"); serve.add_argument("--port", type=int); serve.add_argument("--host")
    for name in ["stop", "status", "doctor", "models", "costs", "mcp", "enroll", "discover", "azure-login"]:
        sub.add_parser(name)
    for name in ["route", "run", "plan"]:
        cmd = sub.add_parser(name); cmd.add_argument("task")
        if name != "route":
            cmd.add_argument("--repo", required=True); cmd.add_argument("--budget", type=float)
            cmd.add_argument("--constraint", action="append", default=[]); cmd.add_argument("--session", default="default")
    cmd = sub.add_parser("execute"); cmd.add_argument("plan_id"); cmd.add_argument("--repo", required=True)
    cmd = sub.add_parser("trace"); cmd.add_argument("request_id")
    cmd = sub.add_parser("cache"); cmd.add_argument("action", choices=["stats", "clear"])
    cmd = sub.add_parser("config"); cmd.add_argument("action", choices=["validate"])
    cmd = sub.add_parser("repo"); cmd.add_argument("action", choices=["add", "list"]); cmd.add_argument("path", nargs="?")
    cmd.add_argument("--check", action="append", default=[], help='Trusted NAME=JSON_ARGV, e.g. tests=["{python}","-m","unittest","discover"]')
    cmd.add_argument("--allow-cloud", action="store_true")
    for name in ["eval", "calibrate"]:
        cmd = sub.add_parser(name); cmd.add_argument("--budget", type=float, default=0); cmd.add_argument("--model")
    cmd = sub.add_parser("demo"); cmd.add_argument("--repo")
    return p

def main():
    args = parser().parse_args()
    try:
        settings = load(args.home, args.mock)
        if args.command == "config":
            output({"valid": True, "models": len(settings.models), "providers": len(settings.providers)})
        elif args.command == "azure-login":
            from .azure_auth import login
            output(login(settings))
        elif args.command == "serve":
            import uvicorn
            from .app import create_app
            if args.port: settings.port = args.port
            if args.host: settings.host = args.host
            app = create_app(settings)
            server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False, log_level="warning"))
            app.state.server = server
            (settings.state/"server.pid").write_text(str(os.getpid()))
            try:
                server.run()
            finally:
                (settings.state/"server.pid").unlink(missing_ok=True)
        elif args.command == "mcp":
            from .mcp_server import create_mcp
            create_mcp(settings).run(transport="stdio")
        elif args.command == "enroll":
            import uvicorn
            from .enroll import create_enrollment
            uvicorn.run(create_enrollment(settings), host="127.0.0.1", port=8766, access_log=False, log_level="warning")
        elif args.command in ("status", "stop"):
            with httpx.Client(trust_env=False, timeout=3) as client:
                url = f"http://127.0.0.1:{settings.port}"
                response = client.get(url+"/health") if args.command == "status" else client.post(url+"/router/stop", headers={"Authorization": "Bearer "+settings.token})
                response.raise_for_status(); output(response.json())
        elif args.command == "repo":
            if args.action == "list":
                output([r.model_dump() for r in settings.repositories]); return
            if not args.path:
                raise ValueError("Repository path required")
            path = Path(args.path).resolve(strict=True)
            if path == Path.home() or path == Path(path.anchor) or not path.is_dir():
                raise ValueError("Register a specific project directory")
            checks = dict(item.split("=", 1) for item in args.check)
            commands = {name: json.loads(argv) for name,argv in checks.items()}
            from .safety import validation_argv
            for name in commands:
                validation_argv(commands, name)
            repo = Repository(path=str(path), validation=commands, allow_cloud=args.allow_cloud)
            repos = [r for r in settings.repositories if Path(r.path).resolve() != path]+[repo]
            (settings.home/"config/repositories.yaml").write_text(yaml.safe_dump({"repositories": [r.model_dump() for r in repos]}, sort_keys=False))
            output({"registered": str(path), "checks": list(commands), "allow_cloud": args.allow_cloud})
        elif args.command == "demo":
            path = Path(args.repo or settings.home/"examples/demo-repo").resolve()
            path.mkdir(parents=True, exist_ok=True)
            settings = load(args.home, mock=True)
            settings.repositories = [Repository(path=str(path), validation={"tests": ["{python}", "-m", "unittest", "discover", "-v"]})]
            async def demo():
                engine = Engine(settings)
                try:
                    return await engine.run(Request(task="Add a greeting feature with named and blank input tests and usage documentation", repo_path=str(path)))
                finally:
                    await engine.close()
            output(asyncio.run(demo()))
        else:
            output(asyncio.run(dispatch(args, settings)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        from .safety import redact
        print(json.dumps({"error": redact(str(exc)), "type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
