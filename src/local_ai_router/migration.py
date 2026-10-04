"""V1 compatibility lives here, outside the control-plane vocabulary."""
from __future__ import annotations

import shutil
import sqlite3
import time
import warnings
from pathlib import Path

import yaml

ROUTING_ALIASES = {
    "laya_enabled": "classifier_enabled",
    "laya_command": "classifier_command",
    "laya_args": "classifier_args",
    "laya_timeout": "classifier_timeout",
    "laya_confidence_threshold": "classifier_confidence_threshold",
    "jev_model": "arbiter_model",
    "jev_risk_threshold": "arbiter_risk_threshold",
    "require_jev_for_critical": "require_arbiter_for_critical",
}


def migrate_routing(raw: dict, warn=True) -> dict:
    data = dict(raw)
    old = [key for key in ROUTING_ALIASES if key in data]
    for key in old:
        value = data.pop(key)
        data.setdefault(ROUTING_ALIASES[key], value)
    if old and "classifier_tool" not in data:
        data["classifier_tool"] = "laya_decide"
    if old and warn:
        warnings.warn("V1 routing keys are deprecated; run router config migrate. " + ", ".join(old), DeprecationWarning, stacklevel=3)
    return data


class LegacyRoutingAccessors:
    """Mutable aliases retained for applications importing V1's Python API."""


def _alias(name):
    def get(self):
        return getattr(self, name)
    def set_(self, value):
        setattr(self, name, value)
    return property(get, set_)


for _old, _new in ROUTING_ALIASES.items():
    setattr(LegacyRoutingAccessors, _old, _alias(_new))


def classifier_reason(code: str) -> list[str]:
    # Trace readers written for V1 can still identify optional classifier outcomes.
    return ["CLASSIFIER_" + code, "LAYA_" + code]


def migrate_files(home: Path) -> dict:
    """Validate the entire migration before backing up and replacing any files."""
    from .safety import repo_lock
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    # Share the configuration writer lock, including preparation and backups.
    with repo_lock(home):
        return _migrate_files_locked(home)


def _migrate_files_locked(home: Path) -> dict:
    from .config import load
    from .safety import digest
    settings = load(home)
    changes = {}
    revisions = {}
    for path in sorted((home / "config").glob("*.yaml")):
        revisions[path] = digest(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("Configuration must be a mapping")
        original = yaml.safe_dump(data)
        if "routing" in data:
            data["routing"] = migrate_routing(data["routing"], warn=False)
        if "repositories" in data:
            for repo in data["repositories"]:
                repo.setdefault("privacy", {"mode": "CLOUD_ALLOWED" if repo.get("allow_cloud") else "LOCAL_ONLY"})
        if yaml.safe_dump(data) != original:
            changes[path] = yaml.safe_dump(data, sort_keys=False)
    version = home / "config" / "version.yaml"
    revisions.setdefault(version, digest(version))
    if not version.exists() or yaml.safe_load(version.read_text(encoding="utf-8")) != {"schema_version": 2}:
        changes[version] = "schema_version: 2\n"
    if not changes:
        return {"schema_version": 2, "changed": [], "backup": None}
    backup = settings.state / "backups" / ("config-v1-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    for path in changes:
        if path.exists():
            shutil.copy2(path, backup / path.name)
    db_path = settings.state / "router.sqlite3"
    if db_path.exists():
        with sqlite3.connect(db_path) as source, sqlite3.connect(backup / "router.sqlite3") as target:
            source.backup(target)
    written = []
    try:
        if any(digest(path) != revision for path, revision in revisions.items()):
            raise ValueError("Configuration changed during migration; retry after other edits finish")
        for path, text in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".yaml.migrating")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
            written.append(path)
        load(home)
    except BaseException:
        for path in written:
            saved = backup / path.name
            if saved.exists():
                shutil.copy2(saved, path)
            else:
                path.unlink(missing_ok=True)
        raise
    return {"schema_version": 2, "changed": [p.name for p in changes], "backup": str(backup)}
