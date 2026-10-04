# Codex setup

Connect Codex to Accension to delegate repository tasks while keeping the host conversation in Codex. Preview the connection first, then apply it to the client you use.

## Companion MCP

```sh
accs integrate codex --mode companion
accs integrate codex --mode companion --apply
accs integrate undo codex
```

Preview inspects the managed mcp_servers.local-ai-router registration. Apply preserves unrelated settings, backs up the target and refuses stale or conflicting content. Undo restores only an unchanged installation. Restart/reload Codex after installation. Register repositories and trusted validation in Accension first.

MCP delegates authorized tasks; it does not replace the host model. Use router_info/router_status for readiness, orchestrate_feature/orchestrate_bugfix for work, and receipt for evidence.

## Sovereign CLI gateway

```sh
accs start --background
accs integrate codex --mode sovereign
accs launch codex --dry-run
accs launch codex
accs launch codex --direct
```

Installed --help/--version output is checked before constructing process-scoped --config overrides. These select an Accension custom Responses provider at the loopback /v1 endpoint, an ACCS_GATEWAY_TOKEN environment reference, requires_openai_auth=false and supports_websockets=false. The launcher supplies the token only to the child process; dry-run does not print it. Sovereign --apply saves a local launcher specification, not global transport replacement.

The client keeps its own tools, filesystem operations and permission model. For Accension-owned compilation use accs run or the task API. Desktop transport is unverified and should use Companion MCP. An eligible Responses-capable downstream deployment is required. Virtual IDs include accension-auto, accension-local, accension-cheap, accension-balanced, accension-quality and accension-planner.

Installed capability checks and mock Responses/SSE tests do not certify every paid deployment or desktop release. Live client inference was not used for this upgrade. For syntax changes, inspect the installed CLI and [official configuration reference](https://developers.openai.com/codex/config-reference/).

Legacy scripts/install_clients.py supports explicit helper/profile/instruction installation and relocation; its uninstall restores only unchanged installed files. It remains separate from the scoped integration command.
