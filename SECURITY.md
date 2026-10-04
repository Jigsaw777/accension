# Security and trust boundaries

## Reporting

Use the repository's **Security > Report a vulnerability** feature when available. Otherwise request a private contact channel without publishing credentials or exploit details. V2 is an early implementation with no production-security or response-time guarantee.

## Local service

Binding is restricted to localhost, 127.0.0.1 or ::1. Public health/static routes expose no provider credentials. GET /ui bootstraps a local session without sign-in. Data routes require that session or the separate API token. Host checks reject DNS-rebinding names; browser writes require same-origin and CSRF validation. Fetch-metadata checks reject cross-site bootstrap. Cookies are HttpOnly and SameSite=Strict.

`accs ui` opens the page directly. API tokens are never placed in URLs or localStorage. Bundled assets use a restrictive Content Security Policy. These checks protect the browser boundary, not against another process running as the same OS user. Remote binding is refused even with --remote; no anonymous remote mode exists. Do not expose the service to untrusted users through a proxy.

## Credentials

Settings contain environment-variable names or vault references. Windows uses DPAPI; macOS/Linux use an optional supported keyring backend with no plaintext fallback. Azure Identity, AWS chains and Google ADC remain provider-native. Credential commands are trusted executables. The local gateway token is separate from downstream provider credentials.

Normalized provider errors avoid echoing upstream bodies. UI validation avoids returning entered secrets. Environment files are not automatically loaded. Keep backups, tokens and vault files private and outside Git.

## Repository execution

Only registered roots can execute tasks. Paths reject traversal, protected directories, reserved Windows forms, symlinks, junctions and hardlinks. Edits check original hashes under a repository lock. Journals are flushed before replacement; rollback checks backups and router-written hashes, preserving later user changes.

Validation uses operator-owned argv arrays, bounded output, timeouts and process-tree termination. Secret-like environment variables are removed. **Commands can still execute repository-controlled code as your OS user.** Plugins and MCP workers are also trusted code, not sandboxes. These controls do not defeat a malicious same-user process racing filesystem operations.

## Egress, spending and integrations

[Privacy](PRIVACY.md) gates run before built-in network/auth paths. Cloud context receives a heuristic secret preflight. Unknown/stale prices fail closed unless estimates are explicitly permitted. Reservations precede inference; uncertain failures retain reservations. Provider billing can differ from configured estimates.

V2 integration writes change only the selected managed entry after preview, backups and stale-file checks. The UI installer does not change PATH or autostart. Legacy setup scripts keep their explicit opt-in behavior.

Run `python scripts/scan_public.py` before sharing changes. Heuristics supplement review and cannot certify that arbitrary content contains no secrets.

AXIR import grants no executable commands: checks must already be registered. Egress reservations are transactional and retain uncertain delivery. Integration undo refuses to overwrite later edits. Sovereign launchers use process-scoped transport settings without replacing global client configuration. See [egress budgets](docs/EGRESS_BUDGETS.md).
