# Security and trust boundaries

Accension runs locally by default. Provider keys stay in your local credential store or environment references. There is no hosted Accension account system. Repository changes use registered paths and checks. Provider plugins are executable code, so install only ones you trust.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/Jigsaw777/accension/security/advisories/new). Do not publish credentials, private code or exploit details in public issues. Accension V1 has no production-security certification or guaranteed response time.

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

Integration writes change only the selected managed entry after preview, backups and stale-file checks. The UI installer does not change PATH or autostart. Legacy setup scripts keep their explicit opt-in behavior.

Run `python scripts/check_local.py --full` before sharing security-sensitive changes. Gitleaks, public-path checks, CodeQL, Bandit, dependency audits and owner review provide several layers of protection. They cannot certify that arbitrary code is harmless. See [GitHub protection and release policy](docs/GITHUB_SECURITY.md).

## Skills and logs

Skills are text guides, separate from executable provider plugins. New skill files need explicit trust. Secret-like contents, excessive size, conflicts, missing dependencies and changed hashes block use. Skills cannot override tools, user constraints, privacy, budgets or a plan's contract. Preset imports cannot grant trust or permissions.

Structured logs keep metadata rather than prompts, source or response bodies. Central redaction removes common credential patterns and sensitive fields. Logs rotate and stay in the configured local home. Diagnostic bundles exclude credentials, configuration files, source, skills and state databases. Review a bundle before sharing it; no redactor recognizes every possible secret format.

AXIR import grants no executable commands: checks must already be registered. Egress reservations are transactional and retain uncertain delivery. Integration undo refuses to overwrite later edits. Sovereign launchers use process-scoped transport settings without replacing global client configuration. See [egress budgets](docs/EGRESS_BUDGETS.md).
