"""Heuristic public-content scan. Reports locations/categories, never secret text."""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider_token": re.compile(
        r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b"
    ),
    "personal_windows_path": re.compile(
        r"[A-Za-z]:[/\\]Users[/\\](?!<|example|USER|your-user|user[/\\])[^/\\\s]+[/\\]", re.I
    ),
    "azure_subscription_value": re.compile(
        r"(?:azure_subscription|subscription_id)\s*[:=]\s*['\"]?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I
    ),
    "literal_credential": re.compile(
        r"(?:api[_-]?key|access_token|password)\s*[:=]\s*['\"][A-Za-z0-9_+/=-]{24,}['\"]", re.I
    ),
}
FORBIDDEN = re.compile(
    r"(?:^|/)(?:\.router|\.venv[^/]*|__pycache__|\.auth|logs)(?:/|$)|(?:^|/)config/local\.yaml$|(?:^|/)\.env(?:\.(?!example$)[^/]*)?$|\.(?:sqlite3?(?:-wal|-shm)?|db(?:-wal|-shm)?|dpapi|pem|key|p12|pfx|jsonl|log)$",
    re.I,
)


def scan():
    raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    paths = sorted(set(raw.decode().split("\0")) - {""})
    findings = []
    for name in paths:
        if FORBIDDEN.search(name):
            findings.append({"file": name, "line": 0, "category": "private_state_file"})
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(lines, 1):
            for category, pattern in RULES.items():
                if pattern.search(line):
                    findings.append({"file": name, "line": number, "category": category})
    return {
        "files_scanned": len(paths),
        "findings": findings,
        "method": "tracked and non-ignored prospective public content; bounded heuristic patterns",
    }


if __name__ == "__main__":
    result = scan()
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result["findings"]))
