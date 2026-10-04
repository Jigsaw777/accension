"""Expose new sensitive Python calls for owner review using exact AST fingerprints."""

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "scripts/security_allowlist.json"
SENSITIVE = {
    "eval",
    "exec",
    "__import__",
    "os.system",
    "os.popen",
    "importlib.import_module",
    "yaml.load",
    "yaml.unsafe_load",
}


def inspect_source(source, filename):
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({n.asname or n.name: n.name for n in node.names})
        elif isinstance(node, ast.ImportFrom):
            aliases.update({n.asname or n.name: (node.module or "") + "." + n.name for n in node.names})
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        def qualified(value):
            if isinstance(value, ast.Name):
                return value.id
            if isinstance(value, ast.Attribute):
                prefix = qualified(value.value)
                return prefix + "." + value.attr if prefix else ""
            return ""

        name = qualified(node.func)
        head, _, tail = name.partition(".")
        resolved = aliases.get(head, head) + ("." + tail if tail else "")
        reasons = []
        if (
            resolved in SENSITIVE
            or resolved.startswith(
                ("subprocess.", "pickle.", "cloudpickle.", "marshal.loads", "asyncio.create_subprocess")
            )
            or name.endswith(".load")
        ):
            reasons.append(resolved)
        if name.endswith((".bind", ".listen")) or resolved in {"socket.socket", "uvicorn.run", "uvicorn.Config"}:
            reasons.append("network-listener")
        if any(
            k.arg in {"shell", "follow_redirects", "allow_redirects"}
            and not (isinstance(k.value, ast.Constant) and k.value.value is False)
            for k in node.keywords
        ):
            reasons.append("shell-or-redirect-policy")
        if name == "print" and any(
            isinstance(n, ast.Name)
            and any(part in n.id.lower() for part in ("password", "secret", "credential", "token"))
            for n in ast.walk(node)
        ):
            reasons.append("credential-output")
        if reasons:
            identity = hashlib.sha256((filename + ast.dump(node, include_attributes=False)).encode()).hexdigest()
            results.append({"id": identity, "file": filename, "line": node.lineno, "categories": reasons})
    return results


def inventory(root=ROOT):
    return [
        finding
        for folder in ("src", "scripts")
        for path in sorted((root / folder).rglob("*.py"))
        for finding in inspect_source(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
    ]


def check(findings, allowed):
    # Count duplicates too: copying an approved sensitive call must trigger review.
    remaining = {item["id"]: item.get("count", 1) for item in allowed if item.get("reason")}
    failures = []
    for item in findings:
        if remaining.get(item["id"], 0) < 1:
            failures.append(item)
        else:
            remaining[item["id"]] -= 1
    return failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", action="store_true", help="Show metadata for manual review; does not approve calls"
    )
    args = parser.parse_args()
    findings = inventory()
    if args.inventory:
        print(json.dumps(findings, indent=2))
    else:
        failures = check(findings, json.loads(ALLOWLIST.read_text()))
        print(json.dumps({"checked": len(findings), "unreviewed": failures}, indent=2))
        raise SystemExit(bool(failures))
