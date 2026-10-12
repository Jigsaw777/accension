# Workbench engineering contracts

Checked against current source on 2026-10-12. This is the active engineering reference; completed construction plans are retained separately in local version history. For user steps, read [the Workbench guide](guides/workbench.md). Local AI planning instructions start at `docs/prompts/latest.json`.

## Runtime and data ownership

`src/local_ai_router/workbench/runtime.py` creates one `WorkbenchRuntime` for an existing Engine. It composes `WorkbenchService`, `Journal`, `Planning`, `Agents`, `EngineAgentAdapter`, `Scheduler`, `Delivery`, `Devices` and `Retention`. Engine retains provider/routing, repository locking, execution checks and receipts. The runtime rejects attaching twice to one Engine.

The domain store uses the same SQLite connection owner through the core Store. Browser/API/CLI/MCP adapters use the domain command service; they must not duplicate business rules or create independent dispatchers. Workbench CLI/MCP calls require the running configured daemon. Management reads must not trigger model inference.

## Schemas, identity and commands

`workbench/schema.py` is the authority for fields, enums and validation. Entity UUIDs and revisions identify records; display keys do not replace workspace-scoped identity. Timestamps, decimal USD strings, bounded prose/lists and exact safe relative paths have explicit validators. Unknown fields and unsafe paths are rejected.

A PRD includes a problem statement, objectives, scope, requirements and acceptance criteria before review/approval. Requirement and criterion IDs must be unique. Approved/current revision matters for downstream planning.

A Work Item needs objective, acceptance criteria and definition of done before READY. ENGINE items additionally need a registered repository, allowed paths, expected artifacts and independent registered validation. Expected artifacts must be a subset of allowed paths. Draft records are not executable contracts.

`WorkbenchService.command` scopes idempotency to workspace, principal and command ID, hashes the kind/workspace/payload, and records the response. Retry with the same ID only for the same request. Existing-entity mutations bind `expected_revision`; refresh on conflict instead of blindly replaying a changed request.

Validated examples are in [docs/examples](examples/README.md). They contain synthetic IDs and require actual workspace/project IDs and registered check names before submission.

## State and completion

The public Work Item states are BACKLOG, READY, ASSIGNED, RUNNING, VERIFYING, REVIEW, DONE, BLOCKED, FAILED, WAITING_FOR_HUMAN and CANCELLED. The service and execution/scheduler code determine permitted transitions; do not update the status column directly or infer a legal transition from this list.

Assignment requires current agent capability/policy eligibility. An execution attempt freezes its contract and policy. Repository artifacts, independent checks, observed receipts and applicable evidence-bound review determine completion. Model output, generated plans and screenshots cannot mark automated work DONE.

Human completion decisions bind the displayed contract revision and evidence hash. Manual work records attestation separately from automated evidence. Missing independent review or stale evidence must not silently become approval. Repository work still needs its artifact and registered-check requirements.

## Privacy, budgets and authority

`workbench/policy.py` derives effective policy from workspace, project, item, repository and source contracts. DEVICE_ONLY and PRIVATE_LAN constrain inference to LOCAL_ONLY. Inherited review requirements use the stricter order AUTO_VERIFY, AI_REVIEW, HUMAN_APPROVAL_REQUIRED. Budget accounting includes spent costs and unsettled attempt reservations; quality requirements cannot be weakened by a child request.

Principals come from the authenticated transport, not payload fields. API service tokens are machine principals without human execution/review approval capabilities. Browser or explicit operator actions use the appropriate human path. Do not manufacture those actions from model output. Workspace grants and `local_only` checks are enforced at command boundaries.

Trusted registered repository checks and enabled provider plugins execute code. They are not sandboxed merely because their configuration passed schema validation. Preserve central endpoint, credential, privacy, path, budget and validation controls.

## Planning and imports

PRD generation/decomposition uses explicit planning operations and applicable quote, privacy and budget checks. Results are proposals; import uses preview/reconciliation and explicit selections. Preserve source plan/node identity and provenance. Import must not overwrite completed history or grant execution authority.

AXIR is separate from Workbench entity identity. It validates repository fingerprints, dependencies, scopes, capabilities and operator-registered validation names. Its execution graph currently contains 1–64 nodes. Export does not authorize arbitrary validators or carry credentials. Read [AXIR](AXIR.md).

## Interfaces, events and remote access

Local API prefixes are `/accs/v1/workbench` and `/ui/workbench/api`, defined in `workbench/api.py`. Thin adapters map resources/commands to the same service. Pagination, workspace scoping and revision/command identities apply across interfaces. Use current route registration and schema code for exact payloads, and [CLI.md](CLI.md#workbench-commands) for command families.

`workbench/events.py` owns the durable journal and snapshots. Ordered events are not interchangeable with coalesced savings/UI notifications. Persist entity revisions and events consistently; handle stale cursors with resynchronization. Do not treat an EventSource reconnect as proof that authorization is still valid.

The separate opt-in LAN app in `workbench/lan.py` shares the runtime and permits an explicit route/asset subset. `devices.py` owns invitations, device credentials, sessions, capabilities and revocation. Require correct HTTPS origin/certificate trust and explicit workspace grants. Local provider, credential, repository and other administrative actions do not become remotely available merely because the peer is on a private IP.

## Recovery and storage

`db_migrations.py` supports schema 9. It rejects unsupported future versions and handles migration locking, integrity and verified backups. Versions 4–7 add Workbench storage, review policy, device trust and workspace-scoped revision identity; version 8 adds activity/model bindings/metrics, and version 9 adds feature delivery/grants/reservations. Read [upgrade notes](UPGRADE_NOTES.md) before touching an existing home.

Backups are private state and may contain paths and credential-related metadata. Stop processes before an explicitly authorized restore; do not replace a live SQLite file or copy it while ignoring committed WAL data. Downgrade restoration loses later database changes without undoing repository files.

Scheduler/adapter recovery distinguishes actual attempt ownership from requested cancellation. RECONCILING and CLEANUP_TIMEOUT require inspection, not an automatic second writer. Resume/rollback verify current policy and file hashes and preserve conflicting human edits. A cleanup timeout cannot be represented as completed cancellation.

Retention is explicit and bounded; it does not imply all execution evidence or audit history expires automatically. Check current `retention.py` before proposing a deletion policy.

## Where to verify changes

Use the [source ownership map](guides/architecture.md#source-map) and the current files under `tests/`. The local AI manuals add a detailed source/test pairing table in `PROJECT_REFERENCE.md`. Relevant suites include domain/store/migration, privacy/review, planning/import, scheduler/execution/recovery, event/sync, CLI/API/MCP, devices/LAN and browser tests. Test existence is not evidence of a fresh passing run.
