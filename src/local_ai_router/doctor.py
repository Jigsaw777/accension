import asyncio, json, os, platform, shutil, sqlite3, sys, tomllib
from pathlib import Path
from .credentials import read_secret

async def doctor(engine):
    s = engine.settings
    providers = {name: await engine.providers.health(p) for name,p in s.providers.items()}
    codex = Path.home()/".codex/config.toml"
    desktop = Path(os.getenv("APPDATA", str(Path.home()/".config")))/"Claude/claude_desktop_config.json"
    claude = Path.home()/".claude.json"
    def registered(path, toml=False):
        if not path.exists():
            return False
        try:
            data = tomllib.loads(path.read_text()) if toml else json.loads(path.read_text(encoding="utf-8-sig"))
            return "local-ai-router" in data.get("mcp_servers" if toml else "mcpServers", {})
        except Exception:
            return False
    try:
        r = await engine.providers.client.get(f"http://127.0.0.1:{s.port}/health", timeout=2)
        gateway = r.status_code == 200 and r.json().get("service") == "local-ai-router"
    except Exception:
        gateway = False
    return {"runtime": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "platform": platform.system()},
      "gateway": {"healthy": gateway, "address": f"http://{s.host}:{s.port}"}, "database": engine.store.db.execute("PRAGMA integrity_check").fetchone()[0],
      "providers": providers, "models": [{"id": m.id, "deployment": m.deployment_name, "tier": m.tier, "enabled": m.enabled,
          "pricing_known": m.input_price is not None and m.output_price is not None, "prompt_cache": m.supports_prompt_cache} for m in s.models],
      "classifier": {"enabled": s.routing.classifier_enabled, "executable_exists": bool(s.routing.classifier_command and Path(s.routing.classifier_command).is_file()), "mode": "optional; deterministic fallback on timeout"},
      "arbiter": engine.router.roles.resolve("arbiter"),
      "control_plane": s.control_plane.model_dump(), "plugin_errors": engine.providers.plugins.errors,
      "clients": {"codex_mcp": registered(codex, True), "codex_profile": (codex.parent/"local-ai-router.config.toml").exists(), "claude_desktop_mcp": registered(desktop), "claude_code_mcp": registered(claude)},
      "tools": {n: bool(shutil.which(n)) for n in ["rtk", "graphify", "git", "codex", "claude", "az"]},
      "skills": {n: Path(p).exists() for n,p in s.skills.items()},
      "repositories": [{"path": r.path, "exists": Path(r.path).is_dir(), "checks": list(r.validation), "cloud_allowed": r.allow_cloud} for r in s.repositories],
      "cache": engine.store.cache_stats(), "costs": engine.store.costs()}
