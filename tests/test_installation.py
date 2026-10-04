import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest


def installer(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "router_installer", Path(__file__).resolve().parents[1] / "scripts/install_clients.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "user"
    home.mkdir()
    new = tmp_path / "new"
    new.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setattr(module, "ROOT", new)
    monkeypatch.setattr(module, "PYTHON", new / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    monkeypatch.setattr(module, "STATE", new / ".router")
    return module, home


def test_installer_migrates_only_matching_entries(monkeypatch, tmp_path):
    module, home = installer(monkeypatch, tmp_path)
    old = tmp_path / "old"
    old_python = str(old / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    command = {"command": old_python, "args": ["-m", "local_ai_router.cli", "--home", str(old), "mcp"]}
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text(
        'model = "keep-this"\n\n# BEGIN LOCAL_AI_ROUTER\n[mcp_servers.local-ai-router]\ncommand = '
        + json.dumps(old_python)
        + "\nargs = "
        + json.dumps(command["args"])
        + "\ntool_timeout_sec = 900\n# END LOCAL_AI_ROUTER\n"
    )
    claude = home / ".claude.json"
    claude.write_text(
        json.dumps(
            {
                "preference": "keep",
                "mcpServers": {
                    "other": {"command": "other"},
                    "local-ai-router": {**command, "env": {"EXAMPLE_SETTING": "keep"}},
                },
            }
        )
    )
    changes, originals = module.build_changes(str(old))
    parsed = tomllib.loads(changes[codex])
    assert parsed["model"] == "keep-this"
    assert parsed["mcp_servers"]["local-ai-router"]["command"] == str(module.PYTHON)
    assert parsed["mcp_servers"]["local-ai-router"]["tool_timeout_sec"] == 900
    data = json.loads(changes[claude])
    assert data["preference"] == "keep" and data["mcpServers"]["other"]["command"] == "other"
    assert data["mcpServers"]["local-ai-router"]["env"] == {"EXAMPLE_SETTING": "keep"}
    assert codex.read_text().find(old_python.replace("\\", "\\\\")) >= 0
    with pytest.raises(RuntimeError, match="another installation"):
        module.build_changes()


def test_installer_does_not_inject_personal_skills(monkeypatch, tmp_path):
    module, home = installer(monkeypatch, tmp_path)
    changes, originals = module.build_changes()
    instructions = changes[home / ".codex/AGENTS.md"]
    assert "<!-- LOCAL_AI_ROUTER -->" in instructions
    assert "ROUTER_USER_SKILL_DEFAULTS" not in instructions


def test_installer_rejects_stale_prepared_changes(monkeypatch, tmp_path):
    module, home = installer(monkeypatch, tmp_path)
    changes, originals = module.build_changes()
    path = home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('model = "user-changed"\n')
    with pytest.raises(RuntimeError, match="since preview"):
        module.apply(changes, originals)
    assert path.read_text() == 'model = "user-changed"\n'
    assert not (home / ".claude.json").exists()


def test_failed_install_keeps_recovery_manifest(monkeypatch, tmp_path):
    module, home = installer(monkeypatch, tmp_path)
    first = home / "first.txt"
    second = home / "second.txt"
    first.write_bytes(b"first original")
    second.write_bytes(b"second original")
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    original_replace = module.os.replace

    def fail_second(source, target):
        if Path(target) == second:
            raise PermissionError("simulated interrupted installation")
        return original_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_second)
    with pytest.raises(PermissionError):
        module.apply({first: "first installed", second: "second installed"}, originals)
    manifests = list(module.STATE.glob("integration-backups/*/manifest.json"))
    assert len(manifests) == 1
    records = json.loads(manifests[0].read_text())
    assert len(records) == 2
    assert first.read_text() == "first installed" and second.read_text() == "second original"
    assert Path(records[0]["backup"]).read_bytes() == b"first original"
    assert not second.with_name(second.name + ".router-new").exists()
