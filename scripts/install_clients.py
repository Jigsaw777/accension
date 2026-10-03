"""Prepare exact config changes; --apply backs up and writes only user-scope additions."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sys, time, tomllib
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
STATE = ROOT / ".router"
MARKER = "# BEGIN LOCAL_AI_ROUTER"
INSTRUCTION = """For nontrivial repository features or multi-file bug fixes in a repository registered with local-ai-router, prefer its orchestrate_feature/orchestrate_bugfix MCP tool. Pass the goal and constraints concisely; it plans, routes, edits and validates. Check router_info status if needed. Report unavailable providers or unregistered roots; never recursively delegate local-ai-router's own development. Preserve user restrictions and approval rules. MCP delegates work; it does not change the host model."""
def build_changes(replace_from=None):
    home = Path.home()
    command = {"command": str(PYTHON), "args": ["-m", "local_ai_router.cli", "--home", str(ROOT), "mcp"]}
    previous_root = Path(replace_from).resolve() if replace_from else None
    previous_python = str(previous_root/".venv"/("Scripts/python.exe" if os.name == "nt" else "bin/python")) if previous_root else None
    changes = {}
    codex = home/".codex/config.toml"
    originals = {}
    def read(path, encoding="utf-8"):
        data = path.read_bytes() if path.exists() else None
        originals[path] = data
        return data.decode(encoding).replace("\r\n", "\n") if data is not None else ""
    original = read(codex)
    parsed = tomllib.loads(original)
    entry = parsed.get("mcp_servers", {}).get("local-ai-router")
    if entry is None:
        changes[codex] = original.rstrip()+"\n\n"+MARKER+"\n[mcp_servers.local-ai-router]\ncommand = "+json.dumps(command["command"])+"\nargs = "+json.dumps(command["args"])+"\nenabled = true\ntool_timeout_sec = 600\n# END LOCAL_AI_ROUTER\n"
    elif entry.get("command") != command["command"]:
        if entry.get("command") != previous_python or entry.get("args") != ["-m", "local_ai_router.cli", "--home", str(previous_root), "mcp"]:
            raise RuntimeError("Existing local-ai-router registration belongs to another installation; use --replace-from with its exact root")
        block = re.search(r"(?ms)^# BEGIN LOCAL_AI_ROUTER\n.*?^# END LOCAL_AI_ROUTER(?:\n|$)", original)
        if not block:
            raise RuntimeError("Existing Codex entry is not a managed block; migrate it manually")
        changed = re.sub(r"(?m)^command = .*?$", lambda _: "command = "+json.dumps(command["command"]), block[0])
        changed = re.sub(r"(?m)^args = .*?$", lambda _: "args = "+json.dumps(command["args"]), changed)
        changes[codex] = original[:block.start()]+changed+original[block.end():]
    profile = home/".codex/local-ai-router.config.toml"
    profile_text = '# Managed by local-ai-router. Separate CLI profile; current defaults stay intact.\nmodel = "router-auto"\nmodel_provider = "local_ai_router"\n\n[model_providers.local_ai_router]\nname = "Local AI Router"\nbase_url = "http://127.0.0.1:8765/v1"\nwire_api = "responses"\nenv_key = "ROUTER_API_TOKEN"\nrequires_openai_auth = false\n'
    original_profile = read(profile)
    if originals[profile] is not None and original_profile != profile_text:
        raise RuntimeError("Existing profile differs; review before replacing")
    changes[profile] = profile_text
    desktop = Path(os.getenv("APPDATA", str(home/".config")))/"Claude/claude_desktop_config.json"
    for path in [desktop, home/".claude.json"]:
        data = json.loads(read(path, "utf-8-sig") or "{}")
        existing = data.setdefault("mcpServers", {}).get("local-ai-router")
        if existing and existing.get("command") not in {command["command"], previous_python}:
            raise RuntimeError("Existing Claude router entry belongs to another installation")
        if existing and existing.get("command") == previous_python and existing.get("args") != ["-m", "local_ai_router.cli", "--home", str(previous_root), "mcp"]:
            raise RuntimeError("Existing Claude arguments differ from the expected old installation")
        data["mcpServers"]["local-ai-router"] = {**(existing or {}), **command}
        changes[path] = json.dumps(data, indent=2)+"\n"
    for path in [home/".codex/AGENTS.md", home/".claude/CLAUDE.md"]:
        text = read(path)
        if "<!-- LOCAL_AI_ROUTER -->" not in text:
            text = text.rstrip()+"\n\n<!-- LOCAL_AI_ROUTER -->\n"+INSTRUCTION+"\n<!-- /LOCAL_AI_ROUTER -->\n"
        changes[path] = text
    return changes, originals

def helpers():
    folder = ROOT/"scripts"
    (folder/"router.cmd").write_text('@echo off\n"'+str(PYTHON)+'" -m local_ai_router.cli --home "'+str(ROOT)+'" %*\n')
    for client in ["codex", "claude"]:
        executable = shutil.which(client)
        if not executable:
            continue
        gateway = ["$RouterRoot = Split-Path $PSScriptRoot -Parent", "$env:ROUTER_API_TOKEN = (Get-Content -LiteralPath \"$RouterRoot/.router/api-token\" -Raw).Trim()"]
        direct = []
        if client == "codex":
            gateway += ["& '"+executable.replace("'", "''")+"' --profile local-ai-router @args"]
            direct += ["& '"+executable.replace("'", "''")+"' @args"]
        else:
            gateway += ["$env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:8765'", "$env:ANTHROPIC_AUTH_TOKEN = $env:ROUTER_API_TOKEN", "$env:ANTHROPIC_MODEL = 'router-auto'", "$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = 'router-cheap'", "$env:ANTHROPIC_DEFAULT_SONNET_MODEL = 'router-balanced'", "$env:ANTHROPIC_DEFAULT_OPUS_MODEL = 'router-quality'", "Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue", "& '"+executable.replace("'", "''")+"' @args"]
            direct += ["Remove-Item Env:ANTHROPIC_BASE_URL,Env:ANTHROPIC_AUTH_TOKEN,Env:ANTHROPIC_MODEL -ErrorAction SilentlyContinue", "& '"+executable.replace("'", "''")+"' @args"]
        for suffix, lines in [("router", gateway), ("direct", direct)]:
            (folder/f"{client}-{suffix}.ps1").write_text("\n".join(lines)+"\n")
            (folder/f"{client}-{suffix}.cmd").write_text('@echo off\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "'+str(folder/f"{client}-{suffix}.ps1")+'" %*\n')

def apply(changes, originals, replace_from=None):
    for path in changes:
        if (path.read_bytes() if path.exists() else None) != originals[path]:
            raise RuntimeError("Configuration changed since preview; prepare changes again")
    folder = STATE/"integration-backups"/(time.strftime("%Y%m%d-%H%M%S")+"-"+uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    records = []
    manifest = folder/"manifest.json"
    def record(change):
        records.append(change)
        temp = folder/"manifest.new"
        temp.write_text(json.dumps(records,indent=2),encoding="utf-8")
        os.replace(temp,manifest)
    for index, (path, text) in enumerate(changes.items()):
        previous = originals[path]
        current = text.encode("utf-8")
        if previous == current:
            continue
        backup = folder/f"{index}.backup"
        if previous is not None:
            backup.write_bytes(previous)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Detect edits made while preparing this individual write.
        if (path.read_bytes() if path.exists() else None) != previous:
            raise RuntimeError("Configuration changed concurrently")
        record({"path":str(path), "backup":str(backup) if previous is not None else None, "installed_sha256":hashlib.sha256(current).hexdigest()})
        temp = path.with_name(path.name+".router-new")
        created = False
        try:
            with temp.open("xb") as f:
                created = True
                f.write(current)
            if (path.read_bytes() if path.exists() else None) != previous:
                raise RuntimeError("Configuration changed concurrently")
            os.replace(temp, path)
        finally:
            if created:
                temp.unlink(missing_ok=True)
    if os.name == "nt":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            try: previous, _ = winreg.QueryValueEx(key,"LocalAIRouter")
            except FileNotFoundError: previous = None
            installed = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+str(ROOT/"scripts/start-router.ps1")+'"'
            record({"registry":"Run/LocalAIRouter", "previous":previous, "installed":installed})
            winreg.SetValueEx(key,"LocalAIRouter",0,winreg.REG_SZ,installed)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try: previous, kind = winreg.QueryValueEx(key,"Path")
            except FileNotFoundError: previous, kind = "", winreg.REG_EXPAND_SZ
            item=str(ROOT/"scripts")
            parts=previous.split(";")
            if replace_from:
                old=str(Path(replace_from).resolve()/"scripts").casefold().rstrip("\\")
                parts=[s for s in parts if s.casefold().rstrip("\\") != old]
            if item.casefold() not in {s.casefold().rstrip("\\") for s in parts}:
                parts.append(item)
            installed=";".join(parts)
            if installed != previous:
                record({"registry":"Environment/Path", "previous":previous, "installed":installed, "kind":kind})
                winreg.SetValueEx(key,"Path",0,kind,installed)
    if not manifest.exists():
        manifest.write_text("[]",encoding="utf-8")
    return {"applied": len(records), "backup_manifest": str(manifest), "autostart": os.name == "nt"}

if __name__ == "__main__":
    p=argparse.ArgumentParser(); p.add_argument("--apply",action="store_true")
    p.add_argument("--replace-from", help="Migrate matching managed registrations from this previous installation root")
    args=p.parse_args()
    changes,originals=build_changes(args.replace_from)
    helpers()
    if args.apply:
        print(json.dumps(apply(changes,originals,args.replace_from),indent=2))
    else:
        print(json.dumps({"files_to_update":[str(x) for x in changes], "autostart":"HKCU Run/LocalAIRouter", "path_addition":str(ROOT/"scripts")},indent=2))
