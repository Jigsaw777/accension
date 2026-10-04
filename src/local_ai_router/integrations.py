"""Scoped MCP registration with reviewable previews and stale-write protection."""
import hashlib
import json
import os
import sys
import time
import tomllib
import shutil
import subprocess
from pathlib import Path
from .schema import uid
from .safety import digest, repo_lock


def target(client):
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "config.toml"
    if client == "claude-code":
        return home / ".claude.json"
    if client == "claude-desktop":
        base = Path(os.getenv("APPDATA", str(home / ".config"))) if sys.platform != "darwin" else home / "Library" / "Application Support"
        return base / "Claude" / "claude_desktop_config.json"
    raise ValueError("Unknown integration")


def prepare(settings, client, path):
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise ValueError("Linked client configuration is not supported")
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    registration = {"command": sys.executable, "args": ["-m", "local_ai_router.cli", "--home", str(settings.home), "mcp"]}
    if client == "codex":
        existing = tomllib.loads(original).get("mcp_servers", {}).get("local-ai-router")
        if existing is not None:
            if existing.get("command") != registration["command"] or existing.get("args") != registration["args"]:
                raise ValueError("Existing router registration differs; review its installation before replacing")
            return original, registration, True
        text = original.rstrip()+"\n\n# BEGIN LOCAL_AI_ROUTER\n[mcp_servers.local-ai-router]\ncommand = "+json.dumps(registration["command"])+"\nargs = "+json.dumps(registration["args"])+"\nenabled = true\ntool_timeout_sec = 600\n# END LOCAL_AI_ROUTER\n"
        tomllib.loads(text)
    else:
        data = json.loads(original or "{}")
        existing = data.setdefault("mcpServers", {}).get("local-ai-router")
        if existing is not None and (existing.get("command") != registration["command"] or existing.get("args") != registration["args"]):
            raise ValueError("Existing router registration differs; review its installation before replacing")
        if existing is not None:
            return original, registration, True
        data["mcpServers"]["local-ai-router"] = registration
        text = json.dumps(data, indent=2) + "\n"
    return text, registration, False


def capabilities(client):
    executable = shutil.which("codex" if client == "codex" else "claude") if client != "claude-desktop" else None
    if not executable:
        return {"installed": False, "sovereign": False, "surface": "desktop" if client == "claude-desktop" else "cli"}
    def read(option):
        try:
            result = subprocess.run([executable, option], capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return result.stdout if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    help, version = read("--help"), read("--version").strip()
    supported = "--config" in help and "--model" in help if client == "codex" else "--model" in help
    return {"installed": True, "version": version, "sovereign": supported, "surface": "cli",
            "desktop_transport": "unverified; use Companion MCP"}


def launcher_spec(settings, client):
    from .service import url
    if client == "codex":
        options = {"model": "accension-auto", "model_provider": "accension",
            "model_providers.accension.name": "Accension", "model_providers.accension.base_url": url(settings) + "/v1",
            "model_providers.accension.env_key": "ACCS_GATEWAY_TOKEN", "model_providers.accension.wire_api": "responses",
            "model_providers.accension.requires_openai_auth": False, "model_providers.accension.supports_websockets": False}
        argv = [item for key, value in options.items() for item in ("--config", key + "=" + json.dumps(value))]
        return {"command": "codex", "args": argv, "environment_names": ["ACCS_GATEWAY_TOKEN"], "endpoint": url(settings)}
    return {"command": "claude", "args": ["--model", "accension-auto"],
            "environment_names": ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"], "endpoint": url(settings)}


def integration_path(engine, client, mode):
    return engine.settings.state / "launch-profiles" / (client + ".json") if mode == "sovereign" else target(client)


def prepared(engine, client, mode, path):
    if mode == "companion":
        return prepare(engine.settings, client, path)
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise ValueError("Linked launcher profile is not supported")
    registration = launcher_spec(engine.settings, client)
    text = json.dumps(registration, indent=2) + "\n"
    return text, registration, path.exists() and path.read_text(encoding="utf-8") == text


def preview(engine, client, mode="companion"):
    if client not in {"codex", "claude-code", "claude-desktop"} or mode not in {"companion", "sovereign"}:
        raise ValueError("Unknown integration or mode")
    detected = capabilities(client) if mode == "sovereign" else None
    requested = mode
    if detected and not detected["sovereign"]:
        mode = "companion"
    path = integration_path(engine, client, mode)
    _, registration, present = prepared(engine, client, mode, path)
    quote = {"id": uid(), "client": client, "path": str(path), "original_hash": digest(path), "registration": registration,
             "already_installed": present, "created_at": time.time(), "status": "preview", "mode": mode, "requested_mode": requested,
             "capabilities": detected, "fallback_reason": "This surface has no verified custom transport; Companion MCP remains supported." if mode != requested else None,
             "next": "accs launch " + client if mode == "sovereign" else "Apply this scoped MCP registration and restart the client"}
    engine.store.metadata("integration:" + quote["id"], quote)
    return quote


def install(engine, quote_id):
    quote = engine.store.metadata("integration:" + quote_id)
    if not quote or quote["status"] != "preview" or time.time() - quote["created_at"] > 900:
        raise ValueError("Integration preview missing, expired or already used")
    mode = quote.get("mode", "companion")
    path = integration_path(engine, quote["client"], mode)
    with repo_lock(engine.settings.home):
        if str(path) != quote["path"] or digest(path) != quote["original_hash"]:
            raise ValueError("Client configuration changed since preview; preview again")
        text, registration, present = prepared(engine, quote["client"], mode, path)
        if registration != quote["registration"]:
            raise ValueError("Installation changed since preview")
        backup = None
        if not present:
            folder = engine.settings.state / "integration-backups" / quote_id
            folder.mkdir(parents=True, exist_ok=False)
            if path.exists():
                backup = folder / "original.backup"
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
            path.parent.mkdir(parents=True, exist_ok=True)
            manifest = folder / "manifest.json"
            record = {"path": str(path), "backup": str(backup) if backup else None, "original_sha256": quote["original_hash"], "installed_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "status": "prepared"}
            with manifest.open("w", encoding="utf-8") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temp = path.with_name(path.name + ".accension-new")
            with temp.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
            try:
                if digest(path) != quote["original_hash"]:
                    raise ValueError("Client configuration changed concurrently")
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            record["status"] = "installed"
            completed = folder / "manifest.complete"
            completed.write_text(json.dumps(record), encoding="utf-8")
            completed.replace(manifest)
        quote["status"] = "installed"
        engine.store.metadata("integration:" + quote_id, quote)
        if not present:
            engine.store.metadata("integration-latest:" + quote["client"], quote_id)
    return {"installed": True, "client": quote["client"], "mode": mode, "already_installed": present, "backup": str(backup) if backup else None, "next": quote.get("next", "Restart the client")}


def list_integrations(engine):
    return {"clients": [{"client": client, "capabilities": capabilities(client),
                         "last_installation": engine.store.metadata("integration-latest:" + client)}
                        for client in ("codex", "claude-code", "claude-desktop")]}


def undo(engine, client):
    quote_id = engine.store.metadata("integration-latest:" + client)
    quote = engine.store.metadata("integration:" + quote_id) if quote_id else None
    if not quote or quote.get("status") != "installed":
        raise ValueError("No managed installation can be undone for this client")
    folder = engine.settings.state / "integration-backups" / quote_id
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    path = integration_path(engine, client, quote.get("mode", "companion"))
    with repo_lock(engine.settings.home):
        if str(path) != manifest["path"] or path.is_symlink() or digest(path) != manifest["installed_sha256"]:
            raise ValueError("Configuration changed after installation; undo refused to preserve later edits")
        backup = folder / "original.backup"
        if manifest["backup"]:
            if backup.is_symlink() or digest(backup) != manifest["original_sha256"]:
                raise ValueError("Integration backup failed its integrity check")
            temporary = path.with_name(path.name + ".accension-restore")
            with temporary.open("xb") as stream:
                stream.write(backup.read_bytes())
            temporary.replace(path)
        else:
            path.unlink()
        quote["status"] = "undone"
        engine.store.metadata("integration:" + quote_id, quote)
    return {"status": "undone", "client": client, "backup_retained": True}


def launch(settings, client, direct=False, arguments=(), dry_run=False):
    client = "claude-code" if client == "claude" else client
    detected = capabilities(client)
    if not detected["installed"]:
        raise ValueError("Client executable unavailable; install it or use Companion MCP")
    if not direct and not detected["sovereign"]:
        raise ValueError("Installed client does not expose the required launcher capabilities; use accs integrate " + client)
    spec = launcher_spec(settings, client)
    command = [shutil.which(spec["command"]), *([] if direct else spec["args"]), *arguments]
    if dry_run:
        return {"mode": "direct" if direct else "sovereign", "command": command,
                "environment_names": [] if direct else spec["environment_names"], "capabilities": detected, "configuration_unchanged": True}
    env = dict(os.environ)
    if not direct:
        from .service import status
        if not status(settings)["running"]:
            raise ConnectionError("Start the local hub first: accs start")
        if client == "codex":
            env["ACCS_GATEWAY_TOKEN"] = settings.token
        else:
            for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                env.pop(name, None)
            env.update(ANTHROPIC_BASE_URL=spec["endpoint"], ANTHROPIC_AUTH_TOKEN=settings.token, ANTHROPIC_MODEL="accension-auto",
                       ANTHROPIC_DEFAULT_OPUS_MODEL="accension-quality", ANTHROPIC_DEFAULT_SONNET_MODEL="accension-balanced", ANTHROPIC_DEFAULT_HAIKU_MODEL="accension-cheap")
    return {"exit_code": subprocess.call(command, env=env), "mode": "direct" if direct else "sovereign"}
