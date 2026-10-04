# Privacy

Accension has no telemetry uploader or hosted control service. Configuration, registry, plans, traces, cache, budgets and benchmarks remain local. Enabled cloud inference sends selected prompts to the configured provider.

| Setting | Boundary |
|---|---|
| Fully Local | Blocks built-in cloud inference, metadata, auth refresh, embeddings, classifier and arbiter |
| Local-only control plane | Routing decisions stay local; permitted cloud models can perform work |
| Repository LOCAL_ONLY | Repository work cannot use cloud providers |
| Repository CLOUD_REDACTED | Excludes protected paths and redacts recognized secrets before cloud use |
| Repository CLOUD_ALLOWED | Allows necessary context, but recognized secrets still block transmission |

Repository scope follows every orchestration role. Custom `privacy.never_send` patterns are repository-relative, such as `secrets/**` and `deployment/private/**`. Sensitive default paths are excluded during context gathering.

Secret detection is heuristic and cannot identify every credential or private fact. Native gateway clients can set `metadata.accension_repository` for an exactly registered root. This routing metadata is removed before forwarding. Without it, global/role policy applies; Accension cannot infer the repository of an arbitrary prompt.

## Trusted local programs

Python plugins, credential commands, MCP processes, validation commands and loopback inference servers run as trusted software. Fully Local governs Accension's built-in egress, not those programs' independent network behavior. An absolute process-level boundary requires an external OS sandbox or firewall.

## Retention and sharing

`.router` can contain source-bearing plans, exact cache entries, rollback backups and OS-encrypted credentials. Traces record structured outcomes, not hidden reasoning. Keep this directory private and ignored. Cache clearing does not delete accounting, plans, backups or registry history.

Profile export contains declarative policy only: no credentials, private endpoints, model IDs, repository paths or executable commands. Import cannot silently relax global or role privacy. No cloud sync or automatic retention deletion is enabled.

AXIR exports task intent without credentials, absolute repository paths or validation argv. Goals, relative filenames and constraints can still be private; inspect before sharing. Receipts omit full prompts and hidden reasoning but retain model IDs, hashes and evidence. Savings, DNA and egress stay in local SQLite. Nothing is automatically uploaded.

Node privacy cannot weaken repository/global restrictions. Local-only dependencies and shared artifacts cannot silently flow into cloud nodes. Context/file budgets reserve before transmission and retain uncertain deliveries. See [egress budgets](docs/EGRESS_BUDGETS.md).
