"""Apply and verify repository security through existing gh authentication.

Default is read-only. Review requirements and non-bypassable checks are separate
rulesets so the owner's PR-only review bypass never bypasses Merge Gate.
"""

import argparse
import base64
import json
import re
import subprocess


def gh(*args, data=None, optional=False):
    result = subprocess.run(
        ["gh", *args],
        input=json.dumps(data) if data is not None else None,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        if optional:
            return None
        # gh output may include authentication diagnostics; never echo raw output.
        raise RuntimeError("GitHub request failed. Check gh authentication and repository admin permissions.")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def api(repo, path="", method="GET", data=None, optional=False):
    args = ["api", "repos/" + repo + path, "--method", method, "-H", "X-GitHub-Api-Version: 2026-03-10"]
    if data is not None:
        args += ["--input", "-"]
    return gh(*args, data=data, optional=optional)


def desired_rules():
    base = {
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    }
    return [
        {
            **base,
            "name": "Protect main",
            "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}],
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "dismiss_stale_reviews_on_push": True,
                        "require_code_owner_review": True,
                        "require_last_push_approval": False,
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["squash"],
                    },
                }
            ],
        },
        {
            **base,
            "name": "Main checks and history",
            "bypass_actors": [],
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {"type": "required_linear_history"},
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "do_not_enforce_on_create": False,
                        "required_status_checks": [{"context": "Merge Gate", "integration_id": 15368}],
                    },
                },
            ],
        },
    ]


def verify(repo):
    settings = api(repo)
    summaries = api(repo, "/rulesets?per_page=100")
    rulesets = [api(repo, "/rulesets/" + str(r["id"])) for r in summaries]
    active = [
        r
        for r in rulesets
        if r.get("enforcement") == "active"
        and r.get("target") == "branch"
        and "refs/heads/main" in r.get("conditions", {}).get("ref_name", {}).get("include", [])
        and not r.get("conditions", {}).get("ref_name", {}).get("exclude")
    ]
    reviews = next((rule["parameters"] for r in active for rule in r["rules"] if rule["type"] == "pull_request"), {})
    invariant = [rule for r in active if not r.get("bypass_actors") for rule in r["rules"]]
    checks = next((r["parameters"] for r in invariant if r["type"] == "required_status_checks"), {})
    content = api(repo, "/contents/.github/CODEOWNERS?ref=main", optional=True)
    owners = base64.b64decode(content["content"]).decode() if content else ""
    result = {
        "repository": repo,
        "main_protected": bool(active),
        "pr_required": bool(reviews),
        "codeowner_required": bool(reviews.get("require_code_owner_review")),
        "owner": "@" + repo.split("/")[0],
        "codeowners_on_main": bool(re.search(r"(?m)^\*\s+@" + re.escape(repo.split("/")[0]) + r"\s*$", owners)),
        "stale_reviews_dismissed": bool(reviews.get("dismiss_stale_reviews_on_push")),
        "conversation_resolution": bool(reviews.get("required_review_thread_resolution")),
        "required_merge_gate": any(
            c["context"] == "Merge Gate" and c.get("integration_id") == 15368
            for c in checks.get("required_status_checks", [])
        ),
        "strict_checks": bool(checks.get("strict_required_status_checks_policy")),
        "force_pushes_blocked": any(r["type"] == "non_fast_forward" for r in invariant),
        "deletion_blocked": any(r["type"] == "deletion" for r in invariant),
        "linear_history": any(r["type"] == "required_linear_history" for r in invariant),
        "bypass_modes": [b["bypass_mode"] for r in active for b in r.get("bypass_actors", [])],
        "squash_only": settings.get("allow_squash_merge")
        and not settings.get("allow_merge_commit")
        and not settings.get("allow_rebase_merge"),
        "discussions": settings.get("has_discussions"),
        "security": settings.get("security_and_analysis", {}),
        "actions": api(repo, "/actions/permissions", optional=True),
        "private_vulnerability_reporting": api(repo, "/private-vulnerability-reporting", optional=True),
    }
    result["bootstrap_note"] = (
        None
        if result["codeowners_on_main"]
        else "CODEOWNERS must land through the reviewed bootstrap PR before GitHub can resolve file owners on main."
    )
    return result


def apply(repo):
    existing = {r["name"]: r for r in api(repo, "/rulesets?per_page=100")}
    for desired in desired_rules():
        old = existing.get(desired["name"])
        api(repo, "/rulesets" + ("/" + str(old["id"]) if old else ""), "PUT" if old else "POST", desired)
    api(
        repo,
        method="PATCH",
        data={
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "allow_auto_merge": False,
            "has_discussions": True,
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
                "dependabot_security_updates": {"status": "enabled"},
            },
        },
    )
    optional = {}
    for path, method, data in [
        ("/vulnerability-alerts", "PUT", None),
        ("/automated-security-fixes", "PUT", None),
        ("/private-vulnerability-reporting", "PUT", None),
        ("/actions/permissions", "PUT", {"enabled": True, "allowed_actions": "all", "sha_pinning_required": True}),
        (
            "/actions/permissions/workflow",
            "PUT",
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
        ),
    ]:
        optional[path] = api(repo, path, method, data, optional=True) is not None
    return {"applied": True, "optional_settings_accepted": optional, "verification": verify(repo)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo")
    args = parser.parse_args()
    repo = args.repo or gh("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Expected OWNER/REPOSITORY")
    if args.apply:
        result = apply(repo)
    elif args.verify:
        result = verify(repo)
    else:
        result = {"dry_run": True, "repository": repo, "rulesets": desired_rules(), "current": verify(repo)}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
