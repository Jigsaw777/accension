# Companion and Sovereign

Companion mode lets your assistant choose when to delegate a task to Accension. Sovereign mode lets Accension plan and execute tasks directly, or route supported calls from a connected client.

| Surface | Control |
|---|---|
| Companion MCP | Host chooses when to delegate; Accension runs the delegated repository task. |
| Sovereign CLI/task API | Accension owns planning, execution, validation and receipt. |
| Sovereign client launcher | Gateway routes supported inference; the client retains its tools, permissions and conversation loop. |

`accs mode companion` or `accs mode sovereign` saves the preferred mode. It does not inject into another app, replace the host model or bypass approvals. Explicit accs run is available in either mode.

Integrate previews by default; --apply installs a backed-up MCP entry or local launcher specification. Undo refuses to overwrite later edits. Launchers use process-scoped configuration/environment; --direct bypasses the gateway.

Codex CLI capability checks precede Responses overrides. Claude Code uses a compatible Messages endpoint. Claude Desktop Sovereign requests fall back to Companion MCP. Transparent desktop interception, universal background-agent routing and graph compilation for every gateway chat are not claimed.

Use `accs launch CLIENT --dry-run` before a real launch. Paid provider/client compatibility needs validation in the intended environment.
