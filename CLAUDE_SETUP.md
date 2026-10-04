# Claude setup

## Companion MCP

```sh
accs integrate claude-desktop --mode companion
accs integrate claude-code --mode companion
accs integrate claude-code --mode companion --apply
accs integrate undo claude-code
```

Apply only the client used. Installation preserves unrelated settings, checks for concurrent edits and backs up the target. Restart the client afterward. MCP keeps the local-ai-router name and delegates registered repository work without replacing Claude's host model/account.

## Claude Code gateway

```sh
accs start --background
accs integrate claude-code --mode sovereign
accs launch claude --dry-run
accs launch claude
accs launch claude --direct
```

The launcher checks installed CLI capabilities and supplies process-scoped ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN and model aliases for the local gateway. An eligible Messages-compatible downstream provider is required. Claude Code retains its tools and permission model; Accension routes inference. The launcher does not alter the global shell environment or saved provider account.

The base-URL/bearer-token mechanism follows [Claude Code's gateway documentation](https://code.claude.com/docs/en/llm-gateway-connect). Environment-only routing is scoped to the launched process; independent editors/background supervisors can use different settings. This release does not promise automatic routing of every Claude surface.

Claude Desktop Sovereign requests explicitly fall back to Companion MCP; transport replacement is not verified. Mock Messages/tools/SSE tests and capability detection do not certify live paid client behavior. Register roots/checks before repository delegation. [Modes](docs/MODES.md) and [MCP](MCP.md) describe the boundary.
