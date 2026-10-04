# Execution compiler and Savings Pulse implementation report

This records the earlier compiler milestone. See [V1 hardening verification](V1_HARDENING_REPORT.md) for the release-candidate checks.

Verified locally on Windows on 2026-10-04 in the existing checkout, alongside the preserved existing features. Personal client settings were not modified. All model execution used deterministic fixtures; there were zero paid inference calls.

## Requested status

| Area | Result |
|---|---|
| Files changed | Core changes span CLI/parser/setup/output/service, contracts/schema/engine/context/providers/gateway, AXIR, DNA, receipts/recovery, SavingsEngine/store/SSE, routing/discovery, Lab, MCP/task API, UI assets, tests and documentation. The checkout also contains earlier development work. Detailed file inventory follows below. |
| CLI entrypoints | `accs` is the primary command. Human output is default; JSON, plain output, debug diagnostics, terminal setup, completion, stable error categories and structured progress are available. |
| Compatibility aliases | `accension`, `router`, Python package `local_ai_router`, and MCP name `local-ai-router` remain. Virtual model selectors include auto, local, cheap, balanced and quality. |
| Default ports | Hub, UI, gateway, native task API and HTTP MCP: `127.0.0.1:8765`. Optional enrollment: `127.0.0.1:8766`. Stdio MCP has no listening port. |
| UI behavior | `/ui` opens without a login code or account. Local browser sessions preserve same-origin/CSRF/Host protection. API clients still authenticate. Remote binding is refused. Savings Pulse remains in the header; themes and narrow layouts are supported. |
| Provider/model scale | No fixed product registry count limit. Separate provider instances preserve account/endpoint identity. Discovery, calibration and inference use bounded work; model browsing/search is paged. Synthetic coverage includes 100 instances and 5,000 models. |
| Companion Mode | Shared-engine MCP route, plan, orchestration, status, receipt, cost and savings surfaces implemented and exercised with mock clients. The host decides when to delegate. |
| Sovereign Mode | `accs run` and the native task API own repository execution. Client launchers route compatible inference through the gateway; the client continues to own its tools and filesystem operations. |
| Codex integration | Preview/apply/undo and process-scoped gateway launcher implemented. Installed `codex-cli 0.160.0` capability check and launcher dry-run passed. Live paid session not run; desktop transport interception is unverified. |
| Claude Code integration | Process-scoped gateway environment and direct bypass implemented. Installed Claude Code `2.1.238` capability check and launcher dry-run passed. Live paid session not run. |
| Claude Desktop integration | Companion MCP preview/apply/undo supported. Unsupported Sovereign desktop configuration returns an explicit Companion fallback. No transport interception claim. |
| AXIR | Version 1 portable envelope, export/import/inspect/diff/reroute and fresh local binding implemented. Current repository fingerprints, dependency cycles, registered checks, capability contracts and stricter privacy are validated. Portable plans carry no credentials or arbitrary shell commands. |
| Model DNA | Typed dimensions, provenance, sample counts, conservative updates, reset and CLI/UI inspection implemented. Verified outcomes and cost evidence influence routing without bypassing quality gates. Unknown and limited evidence remain visible. |
| Execution Receipts | Durable JSON/Markdown evidence includes observed calls, usage/prices, validation, files/hashes, git state, egress and recovery. Finalized economics is immutable; separate repricing is available. This is observable evidence, not remote attestation. |
| Egress Budgets | Transactional request/node context-token and file limits apply through inference and native gateway paths. Dependency privacy taint and repository policy bound node eligibility. Uncertain calls retain reservations. |
| README/documentation | README expanded around execution compilation, modes, CLI, provider scale, privacy and savings. New CLI, AXIR, DNA, receipt, modes, egress and savings guides; existing architecture/configuration/provider/UI/MCP/client/security/contributor guides updated. Eight real browser screenshots added. |
| Verification | Both final full Python suites pass, with package installation/lifecycle, client dry-runs, scale, browser checks and independent accounting fault review described below. |
| Known limitations | USD-only pricing; estimated counterfactual workload/token equivalents; no provider invoice reconciliation or invented local hardware cost. Paid provider/client sessions and non-Windows execution were not run here. Direct remote binding is unsupported. |

## Savings Pulse

The header shows estimated saved cost, paid cloud tokens avoided and percentage when enough evidence exists. Without a baseline it shows local usage; unknown pricing produces unavailable/partial accounting, and negative savings is displayed as above-baseline cost. Details expose current run, today, all time, context/cache factors, method and configurable periods.

The direct comparison uses one normalized equivalent per logical planner, worker and reviewer stage at the selected cloud model's frozen price. Retries and control overhead remain actual costs. This is an estimate of equivalent work, not an observed rerun of the whole feature on another model. Explicit, host, user-selected and quality-based baseline methods are available; none silently chooses an expensive default.

Local tokens, cloud tokens and provider cached tokens stay distinct. Cached tokens are part of cloud usage. Paid tokens avoided are baseline equivalents minus actual cloud usage and can be negative. API cost uses observed/estimated usage and frozen prices; Decimal strings preserve accumulation. Unknown usage or prices never become a fabricated complete total.

SQLite under the configured home's `.router/router.sqlite3` stores facts, frozen prices, receipts, indexed aggregates and accounting-gap markers. Existing state is backed up before additive migration. Finalization atomically updates the receipt and totals once. Failed accounting does not fail inference; durable gaps keep totals unknown after restart until successful recovery. A complete storage outage may prevent recording even that marker.

SSE coalesces local changes at a 250 ms minimum interval and checks cross-process revisions once per second. Header queries use materialized totals and current snapshots instead of scanning historical usage. CLI, UI, MCP, Routing Lab and receipts share the same accounting model. Lab makes zero paid shadow calls.

An actual deterministic demo receipt and `accs savings` agree:

```text
API cost from usage                 USD 0.000000
Estimated direct baseline           USD 0.018970
Estimated savings                  USD 0.018970
Estimated reduction                100.0%
Paid cloud tokens avoided (est.)    1432
Local tokens processed             1432
Cloud tokens processed             0
Provider cached tokens             0
```

These are simulated provider tokens at configured demo prices, not production savings. The fixture made real local file edits and passed registered checks. [Actual command outputs](EXAMPLES.md) include `accs --help`, `accs route`, `accs model dna` and `accs receipt`. [Savings methodology](SAVINGS.md) explains the arithmetic and limitations.

## Exact verification

| Check | Result |
|---|---|
| Python 3.12 full suite | **165 passed**, 4 expected V1 migration deprecation warnings, 106.81 seconds |
| Python 3.11 full suite | **165 passed**, same 4 warnings, 111.03 seconds |
| Latest focused savings/scale/surface suite | **27 passed**, 31.73 seconds |
| Independent bounded recovery review | **5 focused tests passed**; mid-finalization SQLite write failure additionally verified rollback, durable unknown state after restart, recovery and no double-counting |
| Provider scale | Included in full suite: 100 provider instances and 5,000 model records, with bounded discovery and paged browsing |
| Savings scale | Included in full suite: 100,000 runs and 2,000,000 usage records; historical usage-table reads forbidden during header aggregation |
| Wheel | Built with isolated build dependencies; installed into a separate target; import resolved to that installed package; UI and prompt assets present |
| Background service | Installed package passed start → status → restart → status → stop; stopped status returned exit 6 |
| Client launch previews | Codex 0.160.0 and Claude Code 2.1.238 detected; dry-run passed; no client agent session launched |
| Browser | Initial/no-baseline, active and completed cross-process SSE updates, negative savings, unknown price, period switching, light/dark, Escape dismissal and narrow layout inspected; no console errors in the checked tabs |
| Public-content scan | Passed: 153 tracked and non-ignored candidate files, zero findings; heuristic scan, not a security guarantee |
| Whitespace/diff | `git diff --check` passed |
| Not run | Paid inference, live paid client integrations, release publishing, actual macOS/Linux execution, Python 3.13 local run |

Final full-suite commands (Windows):

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest -q --basetemp .router/pytest-acceptance312 --junitxml .router/accs-acceptance312.xml --tb=short
rtk proxy .router\venv311\Scripts\python.exe -m pytest -q --basetemp .router/pytest-acceptance311 --junitxml .router/accs-acceptance311.xml --tb=short
rtk proxy .venv\Scripts\python.exe -m pip wheel --no-deps . --wheel-dir .router/wheel-accs
rtk proxy .venv\Scripts\python.exe scripts/scan_public.py
rtk git diff --check
```

Local JUnit reports and fault/browser fixtures remain ignored under `.router`. Temporary browser tabs and demo servers were closed after capture. Screenshot images were not generated or retouched. [Gallery](screenshots/README.md).

## Practical limits

Counterfactual outputs, cache behavior, retries and tokenizer differences cannot be known without actually running the alternative. Baseline and Lab results remain estimates. Historical receipts retain original prices; repricing does not rewrite them. The conservative budget reservation ledger can differ from analytics for uncertain failed calls.

Discovery is bounded but catalogs and active state still consume memory and storage. Scale tests establish the tested fixture sizes, not unlimited physical capacity. Calibration uses small fixtures and is not production certification. Native gateway compatibility is protocol-specific, not universal cross-provider multimodal translation.

Plugins, configured credential commands, validation commands and MCP processes are trusted local software, not sandboxed. Existing repository registration, allowlists, hash checks, privacy and monetary limits remain in force. Resume requires a valid unfinished checkpoint; finalized runs require a fresh plan.

## File inventory

The following inventory includes preserved existing features already present in the checkout as well as this upgrade. It is not a claim that every file originated in this request.

```text
.github/workflows/tests.yml
ARCHITECTURE.md
CACHE.md
CLAUDE_SETUP.md
CODEX_SETUP.md
CONFIGURATION.md
CONTRIBUTING.md
MCP.md
docs/UPGRADE_NOTES.md
PLUGIN_SDK.md
PRIVACY.md
PROVIDERS.md
README.md
ROUTING_POLICY.md
SECURITY.md
TROUBLESHOOTING.md
UI.md
config/control_plane.yaml
config/examples/local-models.yaml
config/providers.yaml
config/routing.yaml
config/version.yaml
docs/AXIR.md
docs/CLI.md
docs/EGRESS_BUDGETS.md
docs/EXAMPLES.md
docs/EXECUTION_RECEIPTS.md
docs/IMPLEMENTATION_REPORT.md
docs/MODEL_DNA.md
docs/MODES.md
docs/PLUGIN_SDK.md
docs/SAVINGS.md
docs/screenshots/README.md
docs/screenshots/execution-receipt.jpg
docs/screenshots/model-dna.jpg
docs/screenshots/overview-light.jpg
docs/screenshots/overview.jpg
docs/screenshots/route-explanation.jpg
docs/screenshots/routing-policy.jpg
docs/screenshots/savings-mobile.jpg
docs/screenshots/savings-pulse.jpg
examples/provider-plugin/README.md
examples/provider-plugin/accension_example.py
examples/provider-plugin/pyproject.toml
examples/provider-plugin/tests/test_example.py
pyproject.toml
scripts/scan_public.py
src/local_ai_router/__init__.py
src/local_ai_router/app.py
src/local_ai_router/auth.py
src/local_ai_router/axir.py
src/local_ai_router/builtin_providers.py
src/local_ai_router/calibration.py
src/local_ai_router/cli.py
src/local_ai_router/cli_commands.py
src/local_ai_router/cli_output.py
src/local_ai_router/cli_parser.py
src/local_ai_router/cli_setup.py
src/local_ai_router/config.py
src/local_ai_router/conformance.py
src/local_ai_router/context.py
src/local_ai_router/contracts.py
src/local_ai_router/credentials.py
src/local_ai_router/data/prompts/arbiter/v1.txt
src/local_ai_router/data/prompts/classifier/v1.txt
src/local_ai_router/data/prompts/executor/v1.txt
src/local_ai_router/data/prompts/planner/v1.txt
src/local_ai_router/data/prompts/repair/v1.txt
src/local_ai_router/data/prompts/reviewer/v1.txt
src/local_ai_router/data/prompts/synthesizer/v1.txt
src/local_ai_router/decision.py
src/local_ai_router/discovery.py
src/local_ai_router/dna.py
src/local_ai_router/doctor.py
src/local_ai_router/engine.py
src/local_ai_router/errors.py
src/local_ai_router/evaluation.py
src/local_ai_router/gateway.py
src/local_ai_router/integrations.py
src/local_ai_router/lab.py
src/local_ai_router/launcher.py
src/local_ai_router/management.py
src/local_ai_router/mcp_server.py
src/local_ai_router/migration.py
src/local_ai_router/policy.py
src/local_ai_router/privacy.py
src/local_ai_router/probes.py
src/local_ai_router/provider_sdk.py
src/local_ai_router/providers.py
src/local_ai_router/receipts.py
src/local_ai_router/recovery.py
src/local_ai_router/roles.py
src/local_ai_router/routing.py
src/local_ai_router/safety.py
src/local_ai_router/savings.py
src/local_ai_router/savings_stream.py
src/local_ai_router/schema.py
src/local_ai_router/service.py
src/local_ai_router/static/app.js
src/local_ai_router/static/index.html
src/local_ai_router/static/style.css
src/local_ai_router/store.py
src/local_ai_router/task_api.py
src/local_ai_router/transports.py
src/local_ai_router/ui.py
tests/test_accs_core.py
tests/test_accs_surfaces.py
tests/test_provider_conformance.py
tests/test_savings.py
tests/test_savings_scale.py
tests/test_calibration.py
tests/test_control_plane.py
tests/test_management.py
tests/test_recovery.py
```
