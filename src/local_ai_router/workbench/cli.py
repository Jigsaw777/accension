"""Manual Workbench nouns, forwarded to the existing daemon."""

import asyncio
import json
from pathlib import Path

import yaml

from .client import WorkbenchClient
from .store import WorkbenchError, identity

NOUNS = ("workspace", "project", "prd", "work", "workbench", "template", "agent")
RESOURCES = {"workspace": "workspaces", "project": "projects", "prd": "prds", "work": "work-items"}


def add_parsers(command):
    actions = {
        "workspace": ["list", "create", "show", "edit", "archive"],
        "project": ["list", "create", "show", "edit", "archive"],
        "prd": [
            "list",
            "create",
            "show",
            "edit",
            "revisions",
            "submit-review",
            "approve",
            "archive",
            "generate",
            "decompose",
            "reconcile",
        ],
        "work": [
            "list",
            "create",
            "show",
            "edit",
            "revisions",
            "ready",
            "block",
            "unblock",
            "unassign",
            "cancel",
            "retry",
            "reopen",
            "depend",
            "comment",
            "archive",
            "import-preview",
            "import-commit",
            "assign",
            "assignment-preview",
            "approve-execution",
            "run",
            "approve",
            "reject",
            "attempts",
            "receipt",
            "recover",
            "evidence",
            "manual-submit",
            "request-review",
        ],
        "workbench": [
            "status",
            "open",
            "pause",
            "resume",
            "events",
            "privacy",
            "operation",
            "operations",
            "board",
            "models",
            "delivery",
            "delivery-preview",
        ],
        "agent": ["list", "status", "configure", "activity", "metrics"],
        "template": ["list", "preview", "import", "export"],
    }
    for noun in NOUNS:
        parser = command(noun, "Workbench " + noun)
        parser.add_argument("action", choices=actions[noun])
        parser.add_argument("value", nargs="?")
        for option in (
            "workspace",
            "project",
            "name",
            "key",
            "repo-ref",
            "file",
            "output",
            "status",
            "cursor",
            "command-id",
            "on",
            "reason",
            "reason-code",
            "agent",
            "contract-hash",
            "policy-hash",
            "evidence-hash",
            "budget",
            "requirement-file",
            "action",
            "query",
            "from",
            "to",
            "bucket",
            "execution-kind",
            "purpose",
        ):
            parser.add_argument("--" + option)
        parser.add_argument("--expected-revision", type=int)
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--template-version", type=int, default=1)
        parser.add_argument("--contract-revision", type=int)
        parser.add_argument("--after", type=int, default=0)
        parser.add_argument("--wait", action="store_true")


def input_file(path):
    if not path:
        raise ValueError("--file is required")
    with Path(path).open("rb") as stream:
        raw = stream.read(256_001)
    if len(raw) > 256_000:
        raise ValueError("Workbench input exceeds 256 KB")
    if Path(path).suffix.lower() in {".yaml", ".yml"}:
        # Data only: prohibit aliases/tags and bound nesting before safe construction.
        tokens = list(yaml.scan(raw))
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken))
            for token in tokens
        ):
            raise ValueError("YAML tags and aliases are unsupported")
        depth = 0
        for token in tokens:
            if isinstance(
                token,
                (
                    yaml.tokens.BlockMappingStartToken,
                    yaml.tokens.BlockSequenceStartToken,
                    yaml.tokens.FlowMappingStartToken,
                    yaml.tokens.FlowSequenceStartToken,
                ),
            ):
                depth += 1
                if depth > 16:
                    raise ValueError("Input nesting exceeds 16")
            elif isinstance(
                token, (yaml.tokens.BlockEndToken, yaml.tokens.FlowMappingEndToken, yaml.tokens.FlowSequenceEndToken)
            ):
                depth -= 1
        value = yaml.safe_load(raw)
    else:
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Input requires an object")
    return value


async def dispatch(args, settings, *, client=None):
    client = client or WorkbenchClient(settings)
    command_id = args.command_id or identity()
    params = {"workspace_id": args.workspace} if args.workspace else {}
    try:
        noun, action = args.command, args.action
        if noun == "agent":
            if action in {"list", "status", "activity", "metrics"}:
                params.update(limit=args.limit)
                for key in ("cursor", "query", "bucket", "from", "to", "execution_kind"):
                    if getattr(args, key):
                        params[key] = getattr(args, key)
                if args.project:
                    params["project_id"] = args.project
                if action == "status":
                    params.pop("limit")
                path = "/agents" if action == "list" else "/agents/" + args.value
                if action in {"activity", "metrics"}:
                    path += "/" + action
                try:
                    result = await client.request("GET", path, params=params)
                except WorkbenchError as exc:
                    if action == "status" and exc.code == "NOT_FOUND":
                        raise WorkbenchError("NOT_FOUND", "Agent not found", 404) from exc
                    raise
                return result["definition"] if action == "status" else result
            body = {**input_file(args.file), "command_id": command_id}
            if args.expected_revision is not None:
                body["expected_revision"] = args.expected_revision
            result = await client.request(
                "PATCH" if args.value else "POST", "/agents" + ("/" + args.value if args.value else ""), body=body
            )
            return {**result, "command_id": command_id}
        if noun == "workbench":
            if action in {"board", "models", "operations", "delivery", "delivery-preview"}:
                if action == "delivery-preview":
                    body = input_file(args.file)
                    if args.workspace:
                        body["workspace_id"] = args.workspace
                    return await client.request("POST", "/delivery-batches/preview", body=body)
                params.update(limit=args.limit)
                for key, value in (
                    ("project_id", args.project),
                    ("query", args.query),
                    ("cursor", args.cursor),
                    ("purpose", args.purpose),
                ):
                    if value:
                        params[key] = value
                path = {
                    "board": "/board",
                    "models": "/model-options",
                    "operations": "/operations",
                    "delivery": "/delivery-batches/" + (args.value or ""),
                }[action]
                return await client.request("GET", path, params=params)
            if action in {"pause", "resume"}:
                target = args.project or args.workspace
                result = await client.request(
                    "POST",
                    ("/projects/" if args.project else "/workspaces/") + target + "/dispatch",
                    params=params,
                    body={"command_id": command_id, "expected_revision": args.expected_revision, "action": action},
                )
                return {**result, "command_id": command_id}
            if action in {"events", "privacy", "operation"}:
                return await client.request(
                    "GET",
                    "/activity"
                    if action == "events"
                    else "/privacy/diagnostics"
                    if action == "privacy"
                    else "/operations/" + args.value,
                    params={**params, **({"after": args.after, "limit": args.limit} if action == "events" else {})},
                )
            if action == "open":
                import webbrowser

                await client.request("GET", "/status", params=params)
                url = str(client.http.base_url) + "ui/workbench"
                webbrowser.open(url)
                return {"url": url}
            return await client.request("GET", "/status", params=params)
        if noun == "prd" and action in {"generate", "decompose", "reconcile"}:
            body = input_file(args.file) if args.file else {}
            body.update(
                workspace_id=args.workspace or body.get("workspace_id"),
                project_id=args.project or body.get("project_id"),
                budget=args.budget or body.get("budget", "1.00"),
            )
            if action == "reconcile":
                body.pop("budget", None)
                body.update(command_id=command_id, prd_id=args.value, prd_revision=args.expected_revision)
                result = await client.request("POST", "/prds/" + args.value + "/reconcile", body=body)
            else:
                body["kind"] = action
                if action == "generate" and args.requirement_file:
                    with Path(args.requirement_file).open("rb") as stream:
                        raw = stream.read(256001)
                    if len(raw) > 256000:
                        raise ValueError("Requirement exceeds 256 KB")
                    body["requirement"] = raw.decode("utf-8")
                if action == "decompose":
                    body.update(prd_id=args.value, expected_revision=args.expected_revision)
                quote = await client.request("POST", "/planning/quote", body=body)
                result = await client.request(
                    "POST",
                    "/prds/generate" if action == "generate" else "/prds/" + args.value + "/decompose",
                    body={**body, "command_id": command_id, "quote": quote["quote"]},
                )
            return {**await wait_result(client, result, params, args.wait), "command_id": command_id}
        if noun == "work" and action in {"import-preview", "import-commit", "receipt", "recover"}:
            if action == "receipt":
                return await client.request("GET", "/attempts/" + args.value + "/receipt", params=params)
            body = input_file(args.file) if args.file else {}
            body["command_id"] = command_id
            if action == "import-preview":
                body.update(
                    workspace_id=args.workspace or body.get("workspace_id"),
                    operation_id=args.value or body.get("operation_id"),
                )
                result = await client.request("POST", "/plan-imports", body=body)
            else:
                body["expected_revision"] = args.expected_revision
                if action == "recover":
                    body["action"] = args.action or "inspect"
                result = await client.request(
                    "POST",
                    ("/attempts/" + args.value + "/recovery")
                    if action == "recover"
                    else "/plan-imports/" + args.value + "/commit",
                    params=params,
                    body=body,
                )
            return {**await wait_result(client, result, params, args.wait), "command_id": command_id}
        if noun == "template":
            if action == "list":
                return await client.request("GET", "/templates", params=params)
            if action in {"preview", "import"}:
                raw = Path(args.file).read_bytes() if args.file else b""
                if len(raw) > 256_000:
                    raise ValueError("Template exceeds 256 KB")
                body = {"definition": raw.decode("utf-8")}
                if action == "import":
                    body["command_id"] = command_id
                result = await client.request("POST", "/templates/" + action, body=body)
                return {**result, "command_id": command_id} if action == "import" else result
            if not args.value or not args.output:
                raise ValueError("Template export requires ID and --output")
            raw = await client.request(
                "GET", "/templates/" + args.value + "/export", params={"version": args.template_version}
            )
            Path(args.output).write_text(raw, encoding="utf-8")
            return {"path": str(Path(args.output).resolve())}
        resource = RESOURCES[noun]
        if action == "list":
            params.update(limit=args.limit)
            for key, value in (("project_id", args.project), ("status", args.status), ("cursor", args.cursor)):
                if value:
                    params[key] = value
            return await client.request("GET", "/" + resource, params=params)
        if action == "create":
            body = input_file(args.file) if noun in {"prd", "work"} else {"name": args.name}
            body["command_id"] = command_id
            if noun != "workspace":
                body["workspace_id"] = args.workspace or body.get("workspace_id")
            if noun == "project":
                body.update(key=args.key, repository_ref=args.repo_ref)
            if noun in {"prd", "work"}:
                body["project_id"] = args.project or body.get("project_id")
            result = await client.request("POST", "/" + resource, body=body)
        else:
            if not args.value:
                raise ValueError("Entity ID or display key is required")
            entity_id = await client.item_id(args.value, args.workspace) if noun == "work" else args.value
            path = "/" + resource + "/" + entity_id
            if noun == "work" and action in {"assignment-preview", "attempts", "evidence"}:
                return await client.request(
                    "POST" if action == "assignment-preview" else "GET", path + "/" + action, params=params
                )
            if noun == "workspace":
                params = {}
            if action in {"show", "revisions"}:
                return await client.request(
                    "GET", path + ("/revisions" if action == "revisions" else ""), params=params
                )
            if action == "comment":
                body = input_file(args.file)
                body.update(command_id=command_id, workspace_id=args.workspace, item_id=entity_id)
                result = await client.request("POST", "/comments", body=body)
            else:
                if args.expected_revision is None:
                    raise ValueError("--expected-revision is required for mutations")
                body = {"command_id": command_id, "expected_revision": args.expected_revision}
                if noun == "work" and action == "assign":
                    preview = await client.request("POST", path + "/assignment-preview", params=params)
                    result = await client.request(
                        "POST",
                        path + "/assign",
                        params=params,
                        body={
                            **body,
                            "agent_id": args.agent,
                            "policy_revision_hash": args.policy_hash or preview["policy_revision_hash"],
                        },
                    )
                    return {**result, "command_id": command_id}
                if noun == "work" and action in {"approve-execution", "approve", "reject"}:
                    if not args.reason:
                        raise ValueError("--reason is required for human decisions")
                    body.update(
                        command="approve_execution"
                        if action == "approve-execution"
                        else "approve_completion"
                        if action == "approve"
                        else "reject_completion",
                        reason=args.reason,
                    )
                    if action == "approve-execution":
                        preflight = await client.request("POST", path + "/execution-preflight", params=params)
                        body.update(
                            contract_revision=args.contract_revision or preflight["contract"]["contract_revision"],
                            contract_hash=args.contract_hash or preflight["contract_hash"],
                            policy_revision_hash=args.policy_hash or preflight["policy_revision_hash"],
                        )
                    else:
                        evidence = await client.request("GET", path + "/evidence", params=params)
                        body.update(
                            contract_revision=args.contract_revision or evidence["contract_revision"],
                            evidence_hash=args.evidence_hash or evidence["evidence_hash"],
                        )
                    result = await client.request(
                        "POST",
                        "/work-items/operator-command",
                        body={"workspace_id": args.workspace, "item_id": entity_id, "command": body},
                        human_action=True,
                    )
                    return {**result, "command_id": command_id}
                if action == "edit":
                    body = {**input_file(args.file), **body}
                    result = await client.request("PATCH", path, params=params, body=body)
                elif action == "archive" and noun != "prd":
                    result = await client.request("POST", path + "/archive", params=params, body=body)
                elif action == "depend":
                    body["add"] = [await client.item_id(args.on, args.workspace)]
                    result = await client.request("POST", path + "/dependencies", params=params, body=body)
                else:
                    body["command"] = {
                        "submit-review": "submit_review",
                        "run": "request_execution",
                        "request-review": "request_review",
                        "manual-submit": "manual_submit",
                    }.get(action, action)
                    if action == "manual-submit":
                        body.update(input_file(args.file))
                    if action in {"block", "reopen"}:
                        body["reason"] = args.reason
                    if action == "block":
                        body["reason_code"] = args.reason_code or "MANUAL_BLOCK"
                    if noun == "prd" and action == "approve":
                        result = await client.request(
                            "POST",
                            "/prds/operator-approval",
                            body={"workspace_id": args.workspace, "prd_id": entity_id, "command": body},
                            human_action=True,
                        )
                    else:
                        result = await client.request("POST", path + "/commands", params=params, body=body)
        return {**await wait_result(client, result, params, args.wait), "command_id": command_id}
    except WorkbenchError as exc:
        raise WorkbenchError(exc.code, str(exc) + f" Command ID: {command_id}", exc.status) from exc
    finally:
        await client.close()


async def wait_result(client, result, params, enabled):
    if not enabled or "id" not in result or result.get("state") != "PENDING":
        return result
    for _ in range(300):
        result = await client.request("GET", "/operations/" + result["id"], params=params)
        if result["state"] in {"COMPLETED", "FAILED", "UNCERTAIN"}:
            return result
        await asyncio.sleep(1)
    return result
