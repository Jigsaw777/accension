# Contributing

Start with Python 3.11+ and a branch or fork. A Pull Request (PR) proposes your change for review. Small fixes are welcome; explain what changed and why.

## Checks

```sh
python -m pip install -e ".[test,dev]"
python scripts/check_local.py
# Also run online dependency/history audits and clean-wheel checks:
python scripts/check_local.py --full
```

If Windows denies the system pytest temp directory, run pytest with `--basetemp .router/pytest-local`. For the combined script, set `PYTEST_ADDOPTS=--basetemp=.router/pytest-local` in your shell. JavaScript changes also need `node --check src/local_ai_router/static/app.js`. Node is not required at runtime.

Tests use mock providers and credentials. They cover streaming, tools, routing, edits, budgets, migrations, UI sessions, calibration, plugins, recovery, skills and redacted logging. Unexpected external sockets are blocked in the test process. CI tests Windows, Ubuntu and macOS with Python 3.11–3.13. Do not add paid credentials or live model calls to normal CI.

## Submit your PR

Optional: install `pre-commit` and run `pre-commit install` to enable the included quick checks before each commit. CI and the full local check command remain required.

Explain the change, its tests, and any security or privacy impact. Update the relevant guide for visible behavior changes and follow the [style guide](docs/STYLE_GUIDE.md). Preserve compatibility names, native gateway payloads, budget limits, repository locks and rollback guards.

Every contributor PR needs `@Jigsaw777`'s Code Owner review. New commits dismiss stale approvals, review conversations must be resolved, and every required check must pass. Direct pushes, force pushes and deletion of `main` are blocked. Squash merging keeps its history linear. Owner-authored PRs have a PR-only review bypass but cannot bypass checks. See [the exact policy](docs/GITHUB_SECURITY.md).

Run Ruff formatting when changing Python. Changes to sensitive calls also need an explained, reviewed update to `scripts/security_allowlist.json`; never regenerate that list blindly. Keep failures visible and report checks you could not run.

Provider contributions should use [the SDK](docs/PLUGIN_SDK.md) and [example package](examples/provider-plugin). Verify model mapping, usage, errors and redaction with offline fixtures. Keep unknown capabilities and prices conservative.

Never commit local configuration, databases, vault files, personal paths, auth caches, private fixtures or generated plans. Secret scanning is heuristic; inspect the diff too. Document untested platforms and protocols honestly. Security reports follow [SECURITY.md](SECURITY.md). Contributions use Apache-2.0 and preserve third-party notices.

Compiler changes must preserve AXIR validation, privacy/egress intersection, registered checks, stale hashes and receipt integrity. Accounting tests should cover missing facts, retries, negative savings, cache/local token distinctions, frozen prices, failure isolation and restart persistence. Keep header queries indexed and use mock scale fixtures. Browser changes need keyboard, narrow-screen and light/dark checks; screenshot fixtures must be labeled mock.
