# Accension

[![Tests](https://github.com/Jigsaw777/accension/actions/workflows/tests.yml/badge.svg)](https://github.com/Jigsaw777/accension/actions/workflows/tests.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

**A local AI execution compiler: turn a repository task into a capability-constrained plan, run it across eligible models, and keep a receipt of the result.**

Accension contains a gateway, router and orchestrator. Its repository execution path adds a portable task graph, bounded context, per-node privacy and spending contracts, registered validation, and durable evidence.

```text
AI Gateway → AI Router → AI Orchestrator → Accension Execution Compiler
                                               │
            Goal → AXIR plan → model binding → execution → checks → receipt
```

Ordinary gateway chat stays on a direct forwarding path; it does not compile a DAG. Repository compilation is explicit through `accs run`, the task API or MCP orchestration. A simple task can collapse to one worker node; planning and validation may still add overhead.

Use local models, cloud models, or both. Keep routing, budgets, configuration, task graphs and history on your computer. No Accension account, hosted backend, telemetry, particular vendor or local LLM is required. With no connected models, setup, provider configuration, route simulation and diagnostics still work.

## Install and start in the terminal

Requires Python 3.11+. From a downloaded source checkout on Windows, Linux or macOS, choose one:

```sh
pipx install ".[vault]"
# Or:
uv tool install ".[vault]"
```

These commands install the checked-out version; they do not assume a PyPI release. The vault extra supports Keychain/Secret Service on macOS/Linux; Windows uses DPAPI. Environment credential references work without an OS keyring.

```sh
accs init
accs provider add ollama
accs mode sovereign
accs route "Add a greeting feature with tests" --repo . --explain
accs run "Add a greeting feature with tests" --repo . --local-only
```

Setup registers a repository and trusted validation commands. Install/load your own local model or connect a cloud provider. Execution requires an eligible model; management works with zero models.

For hybrid execution, explicitly permit cloud use for that repository:

```sh
accs provider add openrouter --credential-env OPENROUTER_API_KEY --non-interactive
accs repo add . --privacy CLOUD_REDACTED --check 'tests=["{python}","-m","pytest","-q"]'
accs run "Add a greeting feature with tests" --repo . --budget 0.25 --max-cloud-context 12000
```

## Open the local control plane

`accs ui` opens **http://127.0.0.1:8765/ui** without sign-in or sign-out. Machine-local sessions retain same-origin, CSRF, Host and loopback checks. External API clients still use the separate local API token. Remote binding is refused, including `--remote`; use an authenticated tunnel when necessary.

```sh
accs start --background
accs status
accs restart
accs stop
accs serve
```

The background hub needs no administrator rights and installs no autostart. `serve` runs it in the terminal. `ui --no-browser` supports manual opening.

1. **Setup:** discover local models or connect a cloud provider.
2. **Models:** select models, enter missing prices and preview calibration.
3. **Routing:** choose Balanced or Fully Local.
4. **Repositories:** register a root and trusted test/build commands.
5. **Integrations:** preview and install a Codex or Claude connection.

The UI also includes task execution, receipts, Routing Lab, Model DNA, and a persistent Savings Pulse header with light/dark/system themes.

![Accension local control plane, mock demonstration](docs/screenshots/overview.jpg)

Screenshots use a clearly marked mock workspace. Amounts illustrate configured demo prices and deterministic fixtures, not production savings. [Screenshot gallery](docs/screenshots/README.md).

No YAML or Node.js is needed for this flow. A model remains ineligible until its capability, quality, privacy and price requirements are met.

## Compile, execute and inspect

```sh
accs doctor
accs plan "Refactor authentication" --repo . --export auth.axir.json
accs inspect auth.axir.json
accs reroute auth.axir.json
accs execute auth.axir.json --repo .
accs model dna PROVIDER:MODEL
accs receipt RUN_ID --markdown --output receipt.md
accs lab compare RUN_ID
```

`route` makes no model calls or repository edits. `plan` may invoke an eligible model and saves a reviewable DAG. `execute` applies bounded, hash-checked edits and runs registered validation. `run` combines both. Cloud calibration additionally requires `--allow-paid` after reviewing the quote.

The primary CLI is **`accs`**. Compatibility names **`accension`**, **`router`**, Python **`local_ai_router`**, and MCP **`local-ai-router`** remain supported. See [CLI reference](docs/CLI.md) and [V1 migration](MIGRATION_V1_V2.md).

| Command families | Purpose |
|---|---|
| `init`, `doctor`, `start`, `status`, `stop` | Configure and operate the local hub |
| `provider`, `model`, `role`, `calibrate` | Connect models and inspect eligibility/evidence |
| `route`, `plan`, `execute`, `run` | Preview or execute repository work |
| `receipt`, `trace`, `savings`, `lab`, `recovery` | Inspect results, accounting and recovery |
| `integrate`, `launch`, `config`, `plugin`, `completion` | Integrate clients and automate management |

## Execution compiler features

- Deterministic local decisions, generic roles, capability/quality/privacy gates and task-level fallback.
- Protocol-based plugins for cloud and local providers, native credentials and conservative discovery.
- Portable **AXIR v1** task intent, capability requirements, dependency/artifact scopes and privacy/egress/quality contracts. Import validates cycles, source fingerprints and currently registered checks; it grants no arbitrary shell execution.
- **Model DNA** with typed dimensions, sample counts, provenance and uncertainty. Unknown dimensions stay unknown; one passing fixture does not certify a model.
- **Execution Receipts** with observed calls, frozen price/usage economics, file/patch hashes, validation, git state, egress and recovery evidence. No full prompts or hidden reasoning; no remote attestation.
- Budgeted calibration, OS-protected secrets, exact cache, durable recovery, and transactional cloud-context/file limits alongside monetary budgets.

## Companion and Sovereign modes

| Mode | Behavior |
|---|---|
| Companion | The MCP host chooses when to delegate. Accension executes the delegated repository task; the host model stays the conversation interface. |
| Sovereign | `accs run` and the task API own repository execution. Supported client launchers separately route inference through the gateway; those clients still own their tools and filesystem operations. |

```text
Companion
Claude / Codex host → MCP → Accension delegated task
                              ↓
                       plan → models → checks → receipt

Sovereign task execution
accs / native task API → local decision → AXIR → work graph
                                               ↓
                                   local / cloud A / cloud B
                                               ↓
                                       checks → receipt

Client gateway launchers
Codex / Claude Code → localhost gateway → eligible model
```

```sh
accs integrate claude-desktop --mode companion
accs integrate codex --mode sovereign
accs launch codex --dry-run
accs launch codex
accs launch claude
accs launch codex --direct
```

Integrations preview by default. Add `--apply` for a backed-up, conflict-checked installation; `accs integrate undo CLIENT` restores only an unchanged managed installation. Sovereign integration writes a local launcher specification, preserving global client transport settings. Installed CLI capabilities are checked. Claude Desktop uses Companion fallback; desktop transport interception is not claimed.

## Savings Pulse

Choose an explicit registered cloud baseline under **Costs → Savings settings**. No expensive model is silently selected. Equivalent logical stages form the baseline; retries and control overhead remain actual costs.

```sh
accs savings --today
accs savings --all --json
accs savings --run RUN_ID
accs savings --run RUN_ID --reprice
```

Baseline costs, saved dollars and cloud tokens avoided are estimates. Actual API cost uses provider-reported usage and frozen configured prices. Unknown usage/prices stay unknown; negative savings stay negative. Local tokens remain separate from cached cloud tokens. Local API cost is zero; hardware and electricity costs are not invented. Repricing returns separate analysis without changing historical receipts.

Incremental SQLite totals keep header queries independent of historical usage size. Coalesced SSE updates include calls made by another local process. Routing Lab compares historical tasks using current eligible models without paid shadow inference. [Methodology and limits](docs/SAVINGS.md).

Your usage and savings ledger stays on your machine. Providers retain their own usage records for calls sent to them.

## Provider and model scale

Provider instance IDs distinguish accounts, regions and endpoints; model identity includes its instance. No fixed provider/model registry count cap is imposed. Discovery and search are paged, concurrency is bounded, and inventory does not eagerly probe every model with paid calls. Memory, API quotas and storage remain practical limits. Provider groups and include/exclude patterns constrain eligibility.

Regression coverage includes 100 provider instances with 5,000 models and header queries over 100,000 runs with two million usage rows.

The [MCP tools](MCP.md) orchestrate repositories. The native gateway forwards compatible OpenAI/Anthropic protocols; it does not translate arbitrary tool or multimodal requests into Bedrock/Gemini.

## Developer install

```sh
python -m venv .venv
# Activate .venv, or use its Python executable below.
python -m pip install -e ".[test]"
python -m pytest -q
```

On Windows the executable is `.venv/Scripts/python.exe`; on Linux/macOS it is `.venv/bin/python`. Optional extras: `azure`, `aws`, `google`, `vault`. No container, external database or frontend build is required.

`router --mock demo --repo /path/to/empty-demo` runs a deterministic greeting fixture with real tests and no paid inference. It never substitutes mock answers for real providers. CI targets Ubuntu, Windows and macOS with Python 3.11–3.13.

Contribution paths include provider plugins, transport adapters, capability probes, benchmark fixtures, UI, documentation, routing policies and client integrations. [Contributor guide](CONTRIBUTING.md).

## Guides

| Topic | Documentation |
|---|---|
| Design and routing | [Architecture](ARCHITECTURE.md), [routing policy](ROUTING_POLICY.md) |
| Connect and configure | [Providers](PROVIDERS.md), [UI](UI.md), [configuration](CONFIGURATION.md) |
| Trust and privacy | [Security](SECURITY.md), [privacy](PRIVACY.md) |
| Extensions | [Provider SDK](docs/PLUGIN_SDK.md) |
| Clients | [Codex](CODEX_SETUP.md), [Claude](CLAUDE_SETUP.md), [MCP](MCP.md) |
| Execution compiler | [CLI](docs/CLI.md), [AXIR](docs/AXIR.md), [Model DNA](docs/MODEL_DNA.md), [receipts](docs/EXECUTION_RECEIPTS.md) |
| Modes and accounting | [Modes](docs/MODES.md), [egress budgets](docs/EGRESS_BUDGETS.md), [Savings Pulse](docs/SAVINGS.md) |
| Operations | [Migration](MIGRATION_V1_V2.md), [cache](CACHE.md), [troubleshooting](TROUBLESHOOTING.md), [contributing](CONTRIBUTING.md) |

## Current limits

Calibration is small-fixture evidence, not production-quality certification. Savings are counterfactual estimates, without invoice reconciliation or exact cross-vendor tokenization. Optional analytics cannot fail inference; incomplete accounting is surfaced, although a storage outage can prevent even an error marker from persisting. Repository retrieval is lexical/graph based; embedding transports are optional. Bedrock native probes are limited. Plugins, credential commands, MCP processes and validation are trusted software, not sandboxed. Resume requires a verified unfinished checkpoint; finalized runs need a fresh plan. Live paid client/provider combinations require separate validation in the intended environment.

Licensed under [Apache-2.0](LICENSE). Copyright 2026 Sourik Ganguly and Accension contributors. See [third-party notices](THIRD_PARTY_NOTICES.md).
