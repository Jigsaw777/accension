# Workbench tutorial and reference

Workbench organizes projects, requirements and dependent work items. Model routing lives at `/ui`; the Workbench board lives at `/ui/workbench`. Both use the same Engine, configuration and database. The sidebar, logo, typography and light/dark theme belong to one Accension application.

![Current Workbench board with synthetic project and reviewed Engine tasks](screenshots/feature-board-dark-desktop.png)

For new users who have installed Accension. Manual planning needs no model; execution needs a registered repository, installed project dependencies, trusted checks and eligible models. [Documentation index](../README.md) · [Feature delivery](feature-delivery.md).

Use **Greeting demo**, prefix **DEMO**, throughout this walkthrough. The pictured **Greeting feature (demo)** is an isolated fixture of the same workflow. Its mock inference ran real local tests; metrics are demonstration data. See [capture details](screenshots/feature-showcase.md).

## Open the application

Use the Windows **Accension** shortcut, or run `accs ui` in a source installation and choose **Open Workbench**. The default local URLs are `http://127.0.0.1:8765/ui` and `http://127.0.0.1:8765/ui/workbench`. API clients require their own service token; browser sessions use same-origin cookies and CSRF protection.

## Create your first project

1. Select the **Personal** workspace, open **Projects**, and enter *Greeting demo*.
2. Set ticket prefix *DEMO*, save, and select it in the Project filter. The saved project remains after refresh.
3. Open **Board → Create work item**. Enter the title, objective, acceptance criteria and definition of done, then save.
4. Open the ticket to fill its remaining contract: allowed paths, expected artifacts, registered validation, dependencies, capabilities, budget, privacy and review requirements.
5. Mark the item ready only when its contract and prerequisites are complete. Use the returned validation/reason codes to resolve missing fields.

Ticket keys and column states come from stored items. Search matches titles; each column paginates independently. The desktop board scrolls horizontally. At phone widths, **Visible board state** selects a column, including exceptional states. Keyboard users can activate native buttons/selects and use **Skip to workspace**.

## Plan from a PRD

### Write a PRD manually

1. Open **PRDs → Create PRD**. Set title *Greeting function, tests and documentation*.
2. Describe the problem: “Greeting behavior needs a reusable implementation.” Set objective *Greeting verified* and scope *Greeting, tests, usage*.
3. Add requirement **R1**: “Implement greet and its documentation/tests.” Add criterion **A1**: “Named and blank input verified.” Include out-of-scope work and risks where useful. Save the draft.
4. Inspect the saved revision. Choose **submit review** and then **approve** only after reviewing the current requirements. The PRD becomes **APPROVED**; no execution has started.

![Greeting demo PRD editor showing structured requirements and acceptance criteria](screenshots/feature-prd-light-desktop.png)

*Synthetic draft requirements; PRD approval concerns requirements, not execution.*

### Generate a draft with a model

Open **Generate PRD**, enter requirement text, optionally set a title, and choose **Planner model**. Select **Quote inference**. Inspect eligible planners, privacy and quoted cost before **Save** starts generation. This can call a paid provider when cloud inference is permitted. The resulting document is a draft, not automatically approved.

Reopen a running operation rather than submitting duplicate inference. Missing capabilities, prices or budget can prevent a quote. Review generated scope/criteria using the same manual approval steps. Planning costs remain separate from execution.

### Decompose and import

1. On the approved PRD choose **Decompose approved PRD**. Enter allowed paths such as `greeting.py`, `test_greeting.py` and `USAGE.md`, one per line.
2. Choose a planner and **Quote inference**. Review cost/privacy; **Save** starts decomposition and can spend provider resources.
3. Inspect the import preview: requirement coverage, contracts and dependencies. Implementation precedes tests, which precede usage documentation in this example.
4. Choose **Import selected tasks** after reviewing proposals. Tasks appear in **BACKLOG**. Import edits Workbench records, not repository files, and grants no execution authority.

Advanced imports support explicit create/update/keep/archive/successor decisions. AXIR plans have a 64-node limit. Changed requirements/proposals invalidate stale decisions. [Planning contracts](../WORKBENCH_CONTRACTS.md).

## Execute and review

1. In **Accension settings**, register the repository and trusted test/build commands. Connect models, establish their price/capability evidence, and choose the privacy/routing policy.
2. In Workbench **Agents**, configure the built-in Engine worker, inspect its capabilities and eligibility, and assign the ready item. Missing eligibility is displayed rather than bypassed.
3. For one item inspect **Review execution contract**, choose **Approve execution**, review again and **Run approved item**. For imported features use **Review imported feature**, choose tasks/agents, set **Feature maximum USD**, and **Preview feature execution**. Review scope before **Start approved feature**. Execution can change allowed files, run trusted checks and consume provider resources.
4. Inspect the actual attempt, receipt, checks and artifacts under **Runs** or the item detail. A model response or screenshot alone cannot mark automated work done.
5. Approve the exact completion evidence when human review is required, or reject it with a reason. Changed/stale evidence needs a fresh review.

Manual work records human attestation separately from automated receipts. Repository manual work still needs expected artifacts and registered checks. Automatic verification needs successful independent checks; AI review needs an independent eligible reviewer. If required review is unavailable, work waits for a human.

### Dependencies and models

Upstream tasks must reach **DONE** before dependents proceed; **REVIEW** is not enough. Assignment chooses a worker. Its role bindings and current routing rules select eligible models. Primary/fallback choices cannot bypass privacy, capability, quality, price or budget requirements. [Models versus agents](models-and-routing.md).

### Inspect runs, review and performance

Open a ticket or **Runs** to read its attempt, registered checks and artifact hashes. If a check failed, inspect output, correct the cause and review the current contract before a permitted retry. A model's success message is not proof of completion.

![Greeting completion evidence showing actual checks and required human review](screenshots/feature-review-light-desktop.png)

*Real fixture tests passed; this item still awaits human completion review. Model output is mocked.*

Inspect **Evidence for completion approval**, supply a **Command reason**, then **approve_completion** or **reject_completion**. Review binds to the exact contract/evidence; conflicts require refresh and inspection. Accepted completion advances dependent tasks. Feature delivery completes only when required members are done.

In **Agents**, open the worker and choose **Performance**. Compare attempt success with approved delivery; inspect sample counts, duration units and cost provenance. Unknown usage is not zero, estimates are not invoices, and screenshots are not benchmarks. **Activity** provides paged recorded changes, not another execution authority.

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

Run `accs doctor` first. For **BLOCKED**, read the reason: finish upstream work, fill the contract or fix eligibility. For **FAILED**, inspect the attempt before retrying. For **REVIEW**, inspect evidence; for stale approval or **NEEDS_REVIEW**, make a fresh preview. Interrupted/uncertain workers reconcile before held allowance can be reused. [Delivery recovery](feature-delivery.md#troubleshooting) · [Troubleshooting](../../TROUBLESHOOTING.md).

Offline clients keep their displayed view in memory and disable writes. Refresh after reconnect or a retired cursor. A revision conflict requires refreshing current data before retrying; do not reuse an idempotency command ID for changed input. Revoked devices must pair again with host approval.

Cancellation is complete only after worker cleanup and verified rollback. RECONCILING means the known attempt/owner needs inspection, not that a worker stopped. Resume rejects changed files/policy; rollback preserves conflicting human edits. Do not force a second writer after CLEANUP_TIMEOUT. Inspect local service logs and resolve process ownership before restarting.

| Symptom | Next action |
|---|---|
| Old-looking Workbench after installing | Stop an older source hub; run the rebuilt installer; reload the browser |
| No eligible worker/model | Check capability, price, quality, privacy, skills and remaining budget reason codes |
| Revision or evidence conflict | Refresh the item and inspect its latest contract/evidence |
| Remote certificate/session denied | Correct exact certificate trust/origin or inspect host grants/revocation |
| Hub cannot start | Run `accs doctor`; inspect `.router/service.log` in the active profile |

See [Windows operations](windows.md), [architecture](architecture.md), [security](../../SECURITY.md) and [CLI command help](../CLI.md). Workbench execution does not automatically commit, push or merge Git changes.

## From idea to approved feature

For the full feature-batch flow, continue with [feature delivery](feature-delivery.md). It covers exact approval scope, budgets, pause/cancel, changed contracts and interrupted workers without duplicating the walkthrough above.

CLI and MCP expose scoped reads and commands. Ordinary machine calls cannot approve execution/completion. An authenticated human-operated local CLI has explicit item/PRD approval commands; feature-batch approval uses the browser. Paired browsers need live capability grants. Administration stays local. [CLI reference](../CLI.md#workbench-commands).

## Reproducible offline walkthrough

From a contributor checkout with dependencies installed:

```sh
python scripts/demo_workbench.py --feature
```

The script creates a disposable repository/profile, generates a deterministic three-task greeting plan, imports it, approves the batch as a clearly named fixture operator, runs independent unittest checks, reviews completion, and verifies preserved keys/revisions/receipts after restart. It uses no paid provider. It does not authorize unattended execution in your real workspace.

The automated whole-feature test additionally covers a pinned local worker, a mocked OpenAI-compatible cloud worker and a real HTTPS connector with a disposable trusted CA. These prove host routing, verification and boundaries; they do not certify live vendor accounts or physical-device CA onboarding.
