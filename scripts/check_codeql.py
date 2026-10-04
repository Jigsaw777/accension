"""Fail CI on CodeQL security alerts at medium severity or higher, and other errors."""

import argparse
import json
import math
from pathlib import Path


def result_rule(tool, result):
    """Resolve SARIF references within their component, including CodeQL query packs."""
    reference = result.get("rule", {})
    component_ref = reference.get("toolComponent", {})
    component = tool.get("driver", {})
    if component_ref:
        extensions = tool.get("extensions", [])
        index = component_ref.get("index")
        if index is not None:
            if type(index) is not int or not 0 <= index < len(extensions):
                raise ValueError("Invalid SARIF tool component index")
            component = extensions[index]
        else:
            matches = [
                item
                for item in [component, *extensions]
                if any(key in component_ref for key in ("name", "guid"))
                and all(item.get(key) == component_ref[key] for key in ("name", "guid") if key in component_ref)
            ]
            if len(matches) != 1:
                raise ValueError("Unresolved SARIF tool component")
            component = matches[0]
    rules = component.get("rules", [])
    rule_id = result.get("ruleId", reference.get("id"))
    index = reference.get("index", result.get("ruleIndex"))
    if index is not None:
        if type(index) is not int or not 0 <= index < len(rules):
            raise ValueError("Invalid SARIF rule index")
        rule = rules[index]
        if rule_id is not None and rule.get("id") != rule_id:
            raise ValueError("Inconsistent SARIF rule reference")
        return rule
    matches = [rule for rule in rules if rule_id is not None and rule.get("id") == rule_id]
    if len(matches) != 1:
        raise ValueError("Unresolved SARIF rule; cannot confirm its security severity")
    return matches[0]


def findings(document):
    blocked = []
    if not document.get("runs"):
        raise ValueError("SARIF contains no analysis runs")
    for run in document["runs"]:
        if any(item.get("executionSuccessful") is False for item in run.get("invocations", [])):
            raise ValueError("SARIF reports an unsuccessful analysis")
        for result in run.get("results", []):
            rule = result_rule(run.get("tool", {}), result)
            properties = rule.get("properties", {})
            severity = properties.get("security-severity")
            if severity is not None:
                severity = float(severity)
                if not math.isfinite(severity) or not 0 <= severity <= 10:
                    raise ValueError("Invalid SARIF security severity")
            elif "security" in properties.get("tags", []):
                raise ValueError("Security rule is missing its severity")
            level = result.get("level", rule.get("defaultConfiguration", {}).get("level", "warning"))
            if (severity is not None and severity >= 4) or (severity is None and level == "error"):
                blocked.append({"rule": rule["id"], "severity": severity, "level": level})
    return blocked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()
    paths = sorted(Path(args.directory).rglob("*.sarif"))
    if not paths:
        raise SystemExit("CodeQL produced no SARIF report; cannot confirm a passing scan")
    try:
        blocked = [item for path in paths for item in findings(json.loads(path.read_text(encoding="utf-8-sig")))]
    except (ValueError, TypeError, KeyError) as exc:
        raise SystemExit(f"Cannot validate CodeQL report ({type(exc).__name__}); refusing to pass") from None
    print(json.dumps({"reports": len(paths), "blocking_alerts": blocked}, indent=2))
    raise SystemExit(bool(blocked))


if __name__ == "__main__":
    main()
