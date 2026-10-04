"""Run redacted Gitleaks and emit only category, file, commit and status metadata."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def audit(binary, revision="--all", output=None):
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "redacted.json"
        command = [
            str(binary),
            "git",
            ".",
            "--redact=100",
            "--no-banner",
            "--log-level",
            "error",
            "--log-opts=" + revision,
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ]
        result = subprocess.run(command, capture_output=True, timeout=180, check=False)
        if result.returncode not in {0, 1}:
            raise RuntimeError("Gitleaks failed; no clean audit result is available")
        rows = json.loads(report.read_text()) if report.exists() else []
    safe = {
        "scope": revision,
        "scanner": "gitleaks",
        "findings": [
            {"category": r["RuleID"], "file": r["File"], "commit": r.get("Commit", ""), "status": "needs_review"}
            for r in rows
        ],
        "clean": not rows,
    }
    if output:
        Path(output).write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(safe, indent=2))
    return bool(rows)


def self_test(binary):
    # Assembled fake scanner pattern, never an issued credential.
    fixture = 'gitlab_token = "' + "glpat-" + "0123456789abcdefghij" + '"'
    result = subprocess.run(
        [str(binary), "stdin", "--redact=100", "--no-banner", "--log-level", "error"],
        input=fixture.encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 1:
        raise RuntimeError("Gitleaks did not reject the fake credential fixture")
    print("PASS: Gitleaks rejects the fake credential fixture")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary", default=".router/tools/gitleaks.exe" if os.name == "nt" else ".router/tools/gitleaks"
    )
    parser.add_argument("--range", default="--all")
    parser.add_argument("--output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test(args.binary)
    raise SystemExit(audit(args.binary, args.range, args.output))
