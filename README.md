# Accension

[![Tests](https://github.com/Jigsaw777/accension/actions/workflows/tests.yml/badge.svg)](https://github.com/Jigsaw777/accension/actions/workflows/tests.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Route coding tasks across local and cloud models, with explicit budgets and independent checks.

Working Python V1: a localhost model gateway plus a bounded repository planner, task DAG executor, validation/repair loop, SQLite budgets and cache, and stdio MCP server. The Python module and installed MCP integration retain the name `local_ai_router` / `local-ai-router`.

**Try it without cloud credentials:** the demo creates a greeting module, documentation and tests in `examples/demo-repo`, then executes two real Python tests using deterministic mock inference. Mock mode implements this fixture only; it never silently replaces an unavailable real model.

## Quick start

Requires Python 3.12+ and a source checkout. Windows PowerShell:

```powershell
git clone https://github.com/Jigsaw777/accension.git
cd accension
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m local_ai_router.cli demo
```

On Linux/WSL, run `sh setup.sh`, then `.venv/bin/python -m local_ai_router.cli demo`. Use an editable source checkout; standalone wheel distribution is not supported yet.

Real providers start disabled. Configure endpoints, prices and actual deployment IDs in ignored `config/local.yaml`. Entire YAML sections are replaced, not deeply merged. Azure inventory, local inventories, Laya and personal skills are optional. The router automatically accommodates new deployment IDs on supported protocols; unknown prices and unverified capabilities do not authorize spending.

**Bring your own local models:** [local configuration example](config/examples/local-models.yaml) and [provider/classifier setup](PROVIDERS.md#use-other-local-models). Qwen and Laya are not required.

To start the gateway, run `python -m local_ai_router.cli serve` inside the virtual environment. It binds to `http://127.0.0.1:8765`. For generated Windows helpers, use `setup.ps1` below. RTK prefixes in the remaining examples are optional: omit `rtk proxy` if RTK is not installed.

## Commands

```powershell
rtk proxy .\scripts\router.cmd status
rtk proxy .\scripts\router.cmd doctor
rtk proxy .\scripts\router.cmd route "Rename this variable."
rtk proxy .\scripts\router.cmd discover
rtk proxy .\scripts\router.cmd config validate
```

For a real repository, register its exact root and independent test/build commands first. Put the following section in ignored `config/local.yaml`:

```yaml
repositories:
  - path: C:/path/to/project
    allow_cloud: false
    validation:
      tests: ['{python}', '-m', 'pytest', '-q']
```

Then use an eligible configured model:

```powershell
rtk proxy .\scripts\router.cmd run "Add feature X" --repo C:\path\to\project
rtk proxy .\scripts\router.cmd plan "Add feature X" --repo C:\path\to\project
rtk proxy .\scripts\router.cmd execute PLAN_ID --repo C:\path\to\project
```

`plan` persists a typed JSON DAG and readable plan without editing source. `execute` rejects a stale or already executed plan. `run` performs both. Existing unrelated edits stay in place; proposed edits require original content hashes. Set `allow_cloud: true` only for repositories allowed to send source to configured cloud providers. Restart running services after configuration edits.

## Installation and development

`setup.ps1` creates `.venv`, installs dependencies, generates local helpers, previews client changes and starts the gateway. Pass `-InstallClients` to apply backed-up Codex/Claude integrations, user PATH and per-user autostart. `setup.sh` installs the CLI on Linux/WSL; Linux client registration and autostart are manual.

```powershell
rtk proxy powershell -NoProfile -File .\setup.ps1
rtk proxy .\.venv\Scripts\python.exe -m pytest -q
```

The test suite uses mocks and local subprocesses, not paid models. GitHub Actions runs it on Windows and Linux with Python 3.12. `requirements-lock.txt` records a Windows development environment; `pyproject.toml` defines supported ranges. There is no Redis, broker, container requirement, frontend build, or agent framework. SQLite is sufficient for one workstation.

## Where things live

| Purpose | File or directory |
|---|---|
| Routing and quality gates | `config/routing.yaml` |
| Request/session/daily limits | `config/budgets.yaml` |
| Providers and deployment metadata | `config/providers.yaml`, `config/models.yaml` |
| Future deployment inventory | `config/discovery.yaml` |
| Registered repositories and commands | `config/repositories.yaml` |
| Lazy role instructions | `prompts/`, `config/skills.yaml` |
| Cache, usage, profiles, traces | `.router/router.sqlite3` |
| Repository plans and rollback journals | `<repository>/.router/` |
| Optional generated architecture map | `graphify-out/graph.html` (local, ignored) |

Read [ARCHITECTURE.md](ARCHITECTURE.md), [ROUTING_POLICY.md](ROUTING_POLICY.md), [PROVIDERS.md](PROVIDERS.md), [CACHE.md](CACHE.md), [MCP.md](MCP.md), [CODEX_SETUP.md](CODEX_SETUP.md), [CLAUDE_SETUP.md](CLAUDE_SETUP.md), [SECURITY.md](SECURITY.md), and [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## V1 boundaries

Native protocol forwarding preserves tool calls and SSE without translating between Responses, Chat Completions, and Anthropic Messages. Workers propose bounded complete-file replacements; the host runs registered checks. Repository tools/tests execute as the current OS user, so use trusted repositories. Semantic cache, Redis, worktree merging, general shell agents, full Kotlin/Java semantic benchmarks, crash-resume of partially applied runs, and billed-cost reconciliation are not implemented. Gateway compatibility is covered by mock protocol tests; a live Codex/Claude inference conversation has not been verified.

## Contribute and license

This is an early project. Bug reports, documentation improvements, tests and focused pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

Licensed under [Apache-2.0](LICENSE). Copyright 2026 Sourik Ganguly and Accension contributors. Dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). No API keys, personal configuration, private reports, model weights or third-party skill content are included.
