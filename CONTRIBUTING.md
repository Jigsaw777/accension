# Contributing

Use Python 3.11+ and `python -m pip install -e ".[test]"`. Preserve legacy client names, native gateway payloads, budgets, repository locks and rollback guards.

## Checks

```sh
python -m pytest -q
python scripts/scan_public.py
git diff --check
python -m pip wheel . --no-deps --wheel-dir dist
```

If Windows denies the system pytest temp directory, use `--basetemp .router/pytest-local`. Optional JavaScript syntax check: `node --check src/local_ai_router/static/app.js`. Node is not required at runtime.

Tests use mocked providers/auth, native streaming/tools, deterministic routing, bounded edits, budgets, migrations, UI sessions, calibration, plugin contracts and recovery. CI targets Windows, Ubuntu and macOS with Python 3.11–3.13. CI must never require paid credentials.

Provider contributions should use [the SDK](docs/PLUGIN_SDK.md) and [example package](examples/provider-plugin). Verify model mapping, usage, errors and redaction with offline fixtures. Keep unknown capabilities and prices conservative.

Never commit local configuration, databases, vault files, personal paths, auth caches, private fixtures or generated plans. Secret scanning is heuristic; inspect the diff too. Document untested platforms and protocols honestly. Security reports follow [SECURITY.md](SECURITY.md). Contributions use Apache-2.0 and preserve third-party notices.

Compiler changes must preserve AXIR validation, privacy/egress intersection, registered checks, stale hashes and receipt integrity. Accounting tests should cover missing facts, retries, negative savings, cache/local token distinctions, frozen prices, failure isolation and restart persistence. Keep header queries indexed and use mock scale fixtures. Browser changes need keyboard, narrow-screen and light/dark checks; screenshot fixtures must be labeled mock.
