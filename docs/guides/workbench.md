# Workbench tutorial and reference

Workbench organizes projects, requirements and dependent work items. Model routing lives at `/ui`; the Workbench board lives at `/ui/workbench`. Both use the same Engine, configuration and database. The sidebar, logo, typography and light/dark theme belong to one Accension application.

![Current Workbench board with synthetic project and reviewed Engine tasks](screenshots/feature-board-dark-desktop.png)

Follow the [five-stage README walkthrough](../../README.md#workbench) and [feature delivery guide](feature-delivery.md) for PRD generation, model choices, execution approval, metrics and completion review. See the [current screenshots](screenshots/feature-showcase.md) for capture details.

## Open the application

Use the Windows **Accension** shortcut, or run `accs ui` in a source installation and choose **Open Workbench**. The default local URLs are `http://127.0.0.1:8765/ui` and `http://127.0.0.1:8765/ui/workbench`. API clients require their own service token; browser sessions use same-origin cookies and CSRF protection.

## Create your first project

1. Select the **Personal** workspace, open **Projects**, and enter a project name such as *Website maintenance*.
2. Set a short ticket prefix such as *WEB*, save the project, and select it in the Project filter.
3. Open **Board → Create work item**. Enter the title, objective, acceptance criteria and definition of done, then save.
4. Open the ticket to fill its remaining contract: allowed paths, expected artifacts, registered validation, dependencies, capabilities, budget, privacy and review requirements.
5. Mark the item ready only when its contract and prerequisites are complete. Use the returned validation/reason codes to resolve missing fields.

Ticket keys and column states come from stored items. Search matches titles; each column paginates independently. The desktop board scrolls horizontally. At phone widths, **Visible board state** selects a column, including exceptional states. Keyboard users can activate native buttons/selects and use **Skip to workspace**.

## Plan from a PRD

Open **PRDs**, create a document from the bundled template, and fill its requirements. Submit it for review and approve its current revision. Generate/decompose work only after reviewing its quote and privacy/budget constraints. Preview imported proposals before explicitly choosing create/update/keep/archive/successor actions. A generated plan is advice; it does not edit a repository or approve execution. Each execution plan remains bounded by the existing AXIR limit of 64 nodes.

## Execute and review

1. In **Accension settings**, register the repository and trusted test/build commands. Connect models, establish their price/capability evidence, and choose the privacy/routing policy.
2. In Workbench **Agents**, configure the built-in Engine worker, inspect its capabilities and eligibility, and assign the ready item. Missing eligibility is displayed rather than bypassed.
3. Inspect the displayed execution contract, approve the current contract when required, then request execution. Resume dispatch or enable it explicitly when you want queued work to start.
4. Inspect the actual attempt, receipt, checks and artifacts under **Runs** or the item detail. A model response or screenshot alone cannot mark automated work done.
5. Approve the exact completion evidence when human review is required, or reject it with a reason. Changed/stale evidence needs a fresh review.

Manual work records human attestation separately from automated receipts. Repository manual work still needs expected artifacts and registered checks. Automatic verification needs successful independent checks; AI review needs an independent eligible reviewer. If required review is unavailable, work waits for a human.

| State | Meaning |
|---|---|
| BACKLOG / READY | Draft contract / eligible for assignment after prerequisites |
| ASSIGNED / RUNNING | Assigned worker / actual frozen attempt executing |
| VERIFYING / REVIEW | Registered checks / required completion review |
| DONE | Completion criteria and applicable review satisfied |
| BLOCKED / FAILED / WAITING_FOR_HUMAN / CANCELLED | Reason code, failure, escalation or completed cancellation cleanup |

## Pair a second device

LAN is disabled by default. Configure an exact HTTPS origin with a valid issuer/SAN certificate and key. Trust its issuing CA on the client only after checking the fingerprint. Issue a one-time invitation in local Workbench Settings, compare the verification phrase, and approve explicit workspace capabilities. A remote browser gets only those granted actions and can log out. Local provider, credential and repository administration remain unavailable remotely.

![Paired-device Workbench with synthetic review item](screenshots/workbench-remote-dark-mobile.png)

Device Only blocks remote browsing and uses local inference. Private LAN permits paired clients while keeping inference local. Hybrid still respects inherited repository/model policies. A private IP alone does not establish provider trust.

## Recovery and troubleshooting

Offline clients keep their displayed view in memory and disable writes. Refresh after reconnect or a retired cursor. A revision conflict requires refreshing current data before retrying; do not reuse an idempotency command ID for changed input. Revoked devices must pair again with host approval.

Cancellation is complete only after worker cleanup and verified rollback. RECONCILING means the known attempt/owner needs inspection, not that a worker stopped. Resume rejects changed files/policy; rollback preserves conflicting human edits. Do not force a second writer after CLEANUP_TIMEOUT. Inspect local service logs and resolve process ownership before restarting.

| Symptom | Next action |
|---|---|
| Old-looking Workbench after installing | Stop an older source hub; run the rebuilt installer; reload the browser |
| No eligible worker/model | Check capability, price, quality, privacy, skills and remaining budget reason codes |
| Revision or evidence conflict | Refresh the item and inspect its latest contract/evidence |
| Remote certificate/session denied | Correct exact certificate trust/origin or inspect host grants/revocation |
| Hub cannot start | Run `accs doctor`; inspect `.router/service.log` in the active profile |

See [Windows operations](windows.md), [architecture](architecture.md), [security](../../SECURITY.md) and [CLI command help](../../README.md#cli-commands-and-help). Workbench execution does not automatically commit, push or merge Git changes.

## From idea to approved feature

1. Select the workspace and project, open **PRDs → Generate PRD**, enter a rough idea and constraints, choose an eligible registered planner, and quote the planning budget. **Save** authorizes that quoted inference request. A generated document remains a draft. Reloading lets you reopen the existing operation without submitting another request.
2. Review the draft's labeled problem, objectives, scope, requirement IDs and acceptance entries. Save edits, submit review, then approve the current revision. Choose **Decompose approved PRD** with explicit allowed paths and a new planning quote. This produces a task proposal.
3. Review requirement coverage, artifacts, registered checks and dependencies. **Import selected tasks** creates BACKLOG items. Review each task's agent, model/fallback policy, paths and individual allowance before previewing feature execution. **Start approved feature** is a separate human approval over the exact displayed contracts and batch cap.
4. Open the feature delivery entry under PRDs to inspect counts, held allowance, settled ledger debit and uncertain reservations. Pause stops new dispatch; running work can finish. Cancel requests cleanup. Changed contracts, policy, models, grants or expiry require fresh review. A dependent task waits for the required upstream DONE evidence.
5. Review each ticket's host checks and artifact evidence before approving completion. An Engine or connector success is not DONE. Only after required member completion does the delivery become COMPLETED. Reopen feature progress after reload from the same PRD page.

**Agents** separates configured definitions, live host instances and attempt history. Create or edit an agent locally, grant registered repositories/tools/skills, and choose automatic routing or a pinned primary plus numbered fallbacks for each role. Eligibility reasons come from current policy; heartbeat capacity does not prove that a remote worker is reachable. Connector observations show the last exchange and time, with unknown before the first observation.

**Performance** uses frozen attempt identity and reported-call provenance. Success shows its denominator; duration shows sample count and nearest-rank p50/p95. Human-reviewed delivery, review acceptance, reported tokens, usage estimates, budget estimates and connector-reported costs have distinct meanings. Missing usage/prices remain unknown. Graphs have labeled units and a UTC data table. Planning calls remain separate from execution-agent measures.

Board filters preserve workspace/project/search, agent/type/priority and mobile state during refresh. Cards contain metadata, with bounded column pages. Activity and attempts load bounded pages; use older/more controls rather than loading entire history. CLI and MCP offer the same scoped reads; machine clients cannot approve feature execution or completion. Paired LAN browsers use explicit capability grants and CSRF-protected human decisions. Provider, connector and agent administration remain local.

## Reproducible offline walkthrough

From a contributor checkout with dependencies installed:

```powershell
rtk proxy .venv/Scripts/python.exe scripts/demo_workbench.py --feature
```

The script creates a disposable repository/profile, generates a deterministic three-task greeting plan, imports it, approves the batch as a clearly named fixture operator, runs independent unittest checks, reviews completion, and verifies preserved keys/revisions/receipts after restart. It uses no paid provider. It does not authorize unattended execution in your real workspace.

The automated whole-feature test additionally covers a pinned local worker, a mocked OpenAI-compatible cloud worker and a real HTTPS connector with a disposable trusted CA. These prove host routing, verification and boundaries; they do not certify live vendor accounts or physical-device CA onboarding.
