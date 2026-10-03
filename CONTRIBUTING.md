# Contributing

Issues, bug reports, tests, documentation and focused pull requests are welcome. For a substantial change, open an issue describing the problem and intended behavior first.

## Development

Use a source checkout and Python 3.12 or newer:

```sh
python -m venv .venv
# Activate .venv using your shell's command.
python -m pip install -e '.[test]'
python -m pytest -q
```

Tests use mock providers and temporary repositories. They do not require cloud credentials or paid inference. Personal `config/local.yaml` is excluded from test fixtures. Run the complete suite when changing routing, budgets, file edits, MCP or provider protocols. Add regression coverage for behavior changes; do not weaken checks to make a model output pass.

Keep changes small, use existing libraries and preserve bounded retries, independent validation, transaction-based budgets and repository containment. Document new configuration and limitations. Model-generated code is welcome when you understand and verify it.

Never commit `.env`, `.router`, local configuration, keys, model weights, generated launchers, private reports or source from other projects. Use fake credentials and mock endpoints in tests. Report vulnerabilities through the procedure in SECURITY.md rather than posting secrets publicly.

By intentionally submitting a contribution for inclusion, you agree that it is provided under this project's Apache-2.0 license. Only contribute work you have the right to submit. Third-party code must retain its required license and attribution.

## Review checklist

- Explain the concrete problem and resulting behavior.
- Include the relevant verification command and result.
- Identify remaining limitations or compatibility changes.
- Keep provider costs and credentials out of default test execution.

Be respectful, specific and constructive in project discussions.
