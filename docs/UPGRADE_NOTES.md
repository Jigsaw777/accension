# Upgrading early development builds

Early development builds used an older configuration format. Keep the existing home directory. Install Accension 1.0.0 in the same environment, or point a new installation at it with `--home PATH`. Finish active work before restarting the service.

1. Run `router --home PATH config migrate`.
2. Run `router --home PATH config validate`.
3. Open `router --home PATH ui` and inspect Providers, Roles and Repositories.
4. Restart other router/MCP processes to load the new code and settings.

Migration takes the configuration writer lock, validates input, backs up changed files and the existing database, checks for intervening manual edits, then atomically replaces files. It prints the backup path. A repeat is a no-op. SQLite upgrades are additive and retain a pre-upgrade backup.

| Older setting | Current treatment |
|---|---|
| laya_enabled/command/args/timeout/confidence_threshold | Generic classifier settings |
| jev_model/risk_threshold, require_jev_for_critical | Generic arbiter settings |
| allow_cloud | Explicit repository privacy mode |
| Tier | Compatibility hint within a capability/performance profile |
| Provider/model YAML | Preserved with conservative new defaults |
| router / local_ai_router / local-ai-router MCP | Names preserved |
| Registry, usage, reservations and profiles | Retained locally |

Old routing keys continue loading with deprecation warnings. Legacy classifier tool names remain supported. Public defaults include no personal credentials or model choices.

Remote arbitration is not enabled implicitly: defaults keep routing decisions local. An existing cloud arbiter needs explicit hybrid policy and a role permitting cloud use. Preserved models may be ineligible for a new role until capability, quality or price evidence is supplied.

Existing client registrations normally require no changes because module/MCP names are stable. If installation paths change, use the backed-up legacy replacement workflow or explicitly replace the managed entry. Do not overwrite a different registration silently.

Never restore an old database over a running process. Backups can contain local paths, private state and credential references; keep them private.

The public product version is 1.0.0. Configuration schema 2, database schema 3 and versioned cache keys describe internal formats; they are intentionally unchanged.

The default home now uses your user configuration directory. If an early checkout stored settings beside the source, continue using `--home PATH` or `ROUTER_HOME` to select that existing home. No files are moved automatically.
