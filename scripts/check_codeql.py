"""Fail CI on CodeQL security alerts at medium severity or higher, and other errors."""

import argparse
import json
from pathlib import Path


def findings(document):
    blocked = []
    for run in document.get("runs", []):
        rules = {r["id"]: r for r in run.get("tool", {}).get("driver", {}).get("rules", [])}
        for result in run.get("results", []):
            rule = rules.get(result.get("ruleId"), {})
            severity = rule.get("properties", {}).get("security-severity")
            level = result.get("level", rule.get("defaultConfiguration", {}).get("level", "warning"))
            if (severity is not None and float(severity) >= 4) or (severity is None and level == "error"):
                blocked.append({"rule": result.get("ruleId"), "severity": severity, "level": level})
    return blocked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()
    paths = sorted(Path(args.directory).rglob("*.sarif"))
    if not paths:
        raise SystemExit("CodeQL produced no SARIF report; cannot confirm a passing scan")
    blocked = [item for path in paths for item in findings(json.loads(path.read_text(encoding="utf-8")))]
    print(json.dumps({"reports": len(paths), "blocking_alerts": blocked}, indent=2))
    raise SystemExit(bool(blocked))


if __name__ == "__main__":
    main()
