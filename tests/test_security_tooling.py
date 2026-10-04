import base64
import importlib.util
from pathlib import Path

import pytest
import yaml


def module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_security_policy_rejects_unreviewed_primitives_and_duplicates():
    policy = module("security_policy")
    fixture = "import subprocess as sp\nsp.run(['echo', 'fixture'], shell=True)\n"
    findings = policy.inspect_source(fixture, "src/new.py")
    assert len(findings) == 1 and "shell-or-redirect-policy" in findings[0]["categories"]
    assert policy.check(findings, []) == findings
    approved = [{"id": findings[0]["id"], "reason": "Fixture only; demonstrates explicit review"}]
    assert policy.check(findings, approved) == []
    assert policy.check(findings * 2, approved)
    for code in (
        "eval('1')",
        "import pickle\npickle.loads(payload)",
        "import yaml\nyaml.load(data)",
        "import socket\nsocket.socket().bind(('0.0.0.0', 80))",
    ):
        assert policy.inspect_source(code, "src/new.py")


def test_public_scanner_private_paths_and_fake_patterns():
    scan = module("scan_public")
    for path in (
        ".router/state.json",
        "config/local.yaml",
        ".env.local",
        "foo.pem",
        "state.sqlite3-wal",
        "logs/runtime.jsonl",
    ):
        assert scan.FORBIDDEN.search(path)
    assert not scan.FORBIDDEN.search(".env.example")
    assert not scan.FORBIDDEN.search("docs/screenshots/demo.jpg")
    fake = "sk-" + "unit_test_fixture" * 3
    assert scan.RULES["provider_token"].search(fake)


def test_codeql_alert_gate_rejects_security_findings_without_leaking_code():
    checker = module("check_codeql")
    document = {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {"id": "security", "properties": {"security-severity": "4.0"}},
                            {"id": "low", "properties": {"security-severity": "1.0"}},
                            {"id": "correctness", "defaultConfiguration": {"level": "error"}},
                        ]
                    }
                },
                "results": [
                    {"ruleId": name, "message": {"text": "private source excerpt"}}
                    for name in ("security", "low", "correctness")
                ],
            }
        ]
    }
    blocked = checker.findings(document)
    assert [row["rule"] for row in blocked] == ["security", "correctness"]
    assert "private source" not in str(blocked)
    with pytest.raises(ValueError, match="no analysis runs"):
        checker.findings({"runs": []})


@pytest.fixture
def codeql_report():
    # Shape of the PR's actual CodeQL SARIF: query-pack rules live in extensions,
    # not driver.rules, and result.level can be omitted in favor of the rule default.
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {"name": "CodeQL", "rules": []},
                    "extensions": [
                        {"name": "codeql-action/pr-diff-range", "rules": []},
                        {
                            "name": "codeql/python-queries",
                            "rules": [
                                {
                                    "id": rule,
                                    "properties": {"security-severity": "7.5", "tags": ["security"]},
                                    "defaultConfiguration": {"level": level},
                                }
                                for rule, level in (("py/path-injection", "error"), ("py/polynomial-redos", "warning"))
                            ],
                        },
                    ],
                },
                "results": [
                    {
                        "ruleId": rule,
                        "rule": {"id": rule, "index": index, "toolComponent": {"index": 1}},
                        "message": {"text": "private source excerpt"},
                    }
                    for index, rule in enumerate(("py/path-injection", "py/polynomial-redos"))
                    for _ in range(4)
                ],
            }
        ],
    }


def test_codeql_gate_blocks_real_extension_format(codeql_report, tmp_path, monkeypatch, capsys):
    import json

    checker = module("check_codeql")
    blocked = checker.findings(codeql_report)
    assert len(blocked) == 8
    assert {row["severity"] for row in blocked} == {7.5}
    assert {row["level"] for row in blocked} == {"error", "warning"}
    report = tmp_path / "python.sarif"
    report.write_text(json.dumps(codeql_report), encoding="utf-8-sig")
    monkeypatch.setattr("sys.argv", ["check_codeql", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        checker.main()
    assert exc.value.code == 1
    assert "private source" not in capsys.readouterr().out
    codeql_report["runs"][0]["results"] = []
    assert checker.findings(codeql_report) == []


def test_codeql_references_stay_scoped_to_their_component(codeql_report):
    checker = module("check_codeql")
    run = codeql_report["runs"][0]
    # A driver rule with the same ID must not override the query-pack severity.
    run["tool"]["driver"]["rules"] = [{"id": "py/path-injection", "properties": {"security-severity": "0"}}]
    result = run["results"][0]
    result["rule"]["toolComponent"] = {"name": "codeql/python-queries"}
    del result["rule"]["index"]
    assert len(checker.findings(codeql_report)) == 8
    run["results"] = [{"ruleIndex": 0}]
    assert checker.findings(codeql_report) == []


@pytest.mark.parametrize(
    "fault", ["component", "rule_index", "unknown_rule", "mismatch", "severity", "missing_severity", "failed_scan"]
)
def test_codeql_gate_refuses_incomplete_or_invalid_metadata(codeql_report, fault):
    checker = module("check_codeql")
    run = codeql_report["runs"][0]
    result = run["results"][0]
    rule = run["tool"]["extensions"][1]["rules"][0]
    if fault == "component":
        result["rule"]["toolComponent"]["index"] = -1
    elif fault == "rule_index":
        result["rule"]["index"] = 100
    elif fault == "unknown_rule":
        result["rule"] = {}
    elif fault == "mismatch":
        result["ruleId"] = "missing"
    elif fault == "severity":
        rule["properties"]["security-severity"] = "NaN"
    elif fault == "missing_severity":
        del rule["properties"]["security-severity"]
    else:
        run["invocations"] = [{"executionSuccessful": False}]
    with pytest.raises(ValueError):
        checker.findings(codeql_report)


def test_github_rules_owner_bypass_cannot_skip_checks_or_push_directly():
    security = module("configure_github_security")
    review, integrity = security.desired_rules()
    assert review["enforcement"] == integrity["enforcement"] == "active"
    assert review["bypass_actors"] == [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}]
    assert integrity["bypass_actors"] == []
    rule = review["rules"][0]["parameters"]
    assert rule["required_approving_review_count"] == 1 and rule["require_code_owner_review"]
    assert rule["dismiss_stale_reviews_on_push"] and rule["required_review_thread_resolution"]
    assert {r["type"] for r in integrity["rules"]} == {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "required_status_checks",
    }


def test_github_apply_is_idempotent_and_verifies_server_state(monkeypatch):
    security = module("configure_github_security")
    rules, changes = {}, []
    settings = {
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "has_discussions": True,
    }

    def api(repo, path="", method="GET", data=None, optional=False):
        if method != "GET":
            changes.append((path, method, data))
            if path == "/rulesets":
                rules[len(rules) + 1] = data
            elif path.startswith("/rulesets/"):
                rules[int(path.rsplit("/", 1)[1])] = data
            return {}
        if path == "/rulesets?per_page=100":
            return [{"id": key, "name": value["name"]} for key, value in rules.items()]
        if path.startswith("/rulesets/"):
            return rules[int(path.rsplit("/", 1)[1])]
        if path.startswith("/contents"):
            return {"content": base64.b64encode(b"* @Jigsaw777\n").decode()}
        return settings if not path else {}

    monkeypatch.setattr(security, "api", api)
    first = security.apply("Jigsaw777/accension")
    second = security.apply("Jigsaw777/accension")
    assert len(rules) == 2
    assert sum(method == "POST" for _, method, _ in changes) == 2
    result = second["verification"]
    for key in (
        "main_protected",
        "pr_required",
        "codeowners_on_main",
        "required_merge_gate",
        "strict_checks",
        "force_pushes_blocked",
        "deletion_blocked",
        "linear_history",
        "squash_only",
    ):
        assert result[key], key
    assert first["verification"] == result


def test_workflow_all_required_checks_join_gate_and_no_fork_secrets():
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/tests.yml").read_text())
    jobs = ci["jobs"]
    assert set(jobs["merge-gate"]["needs"]) == set(jobs) - {"merge-gate"}
    assert jobs["merge-gate"]["if"] == "always()"
    assert jobs["test"]["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13"]
    assert len(jobs["test"]["strategy"]["matrix"]["os"]) == 3
    assert "pull_request_target" not in str(ci)
    assert "secrets." not in str(ci)
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert len(step["uses"].rsplit("@", 1)[1]) == 40
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False


def test_test_network_guard_blocks_external_connections():
    import socket

    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="external network"):
            sock.connect(("203.0.113.1", 443))
