# Security and trust boundaries

## Reporting a vulnerability

Use the repository's **Security > Report a vulnerability** feature for private reports when available. Do not post exploit details, credentials or private source in a public issue. If private reporting is unavailable, open an issue requesting a private contact channel without disclosing the vulnerability. Version 0.1 is experimental; no response-time or production-security guarantee is provided.

The gateway binds to `127.0.0.1:8765` by default. A generated local API token protects all data and mutation routes. `/health` and the token-entry HTML shell are public on loopback. Cross-origin browser requests and recursive router hops are rejected. Native provider requests use separate credentials, not the local client token. Do not expose this service on a public interface.

Cloud credentials come from environment variables, optional Azure Identity encrypted persistence, or Windows DPAPI files. The repository contains no provider credentials. `.env` is ignored and `.env.example` contains names only; V1 does not automatically load `.env`. Explicitly set environment variables in the process starting the server. Provider errors avoid returning upstream bodies and tokens.

Repository roots must be registered. File paths are checked for traversal, protected directories, Windows reserved forms, symlinks, junctions and hardlinks. Edits use original hashes and exclusive temporary files. A per-repository OS lock avoids concurrent router runs. Failed/cancelled work restores only unchanged router-owned output. These controls reduce mistakes; they are not protection against a malicious same-user process racing filesystem checks.

Validation commands are operator-owned argv arrays, never generated shell strings. Known destructive shell/Git forms are rejected. Checks run with secret-like environment variables removed, no inherited MCP input, bounded log tails and process-tree termination on timeout. **A trusted command can still execute repository-controlled code as your OS user.** Run only trusted repositories or add an external OS/container sandbox. A passing model-generated test alone is not independent assurance; register existing build/lint/regression checks.

Secret-like source and requests are blocked before orchestration sends them to models. Redaction is heuristic and cannot recognize every secret. The native gateway forwards explicit client input and does not inspect/redact arbitrary content, because doing so could alter tool protocols. Configure cloud access according to the repository's sensitivity. Telemetry omits full prompts/source by default, but plans, exact caches and rollback backups are local sensitive artifacts.

Money is reserved transactionally before inference. Unknown pricing blocks routing, uncertain failures retain their reservation, and cache-write prices must be explicit when caching is enabled. These are estimates, not provider-enforced credit limits. Verify price configuration and unexpected billing independently. There is no automatic quota purchase, deployment creation, public deployment or machine-wide persistence.

Autostart uses the current user's HKCU Run entry only. Integration files and registry values are backed up before modification. No credentials, cache database, generated API token or local backups belong in Git.
