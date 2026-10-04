# Provider plugin SDK, API version 1

A provider plugin adds support for another AI service. This developer guide explains how to build and test one. Skills are instruction files; provider plugins are executable Python code.

Packages register entry points in `accension.providers`. Entry point name, manifest ID and enabled plugin name must match. Incompatible/broken plugins are reported without preventing management startup.

Plugins execute as the current OS user. **Only install and enable trusted code; plugins are not sandboxed.** Manifest fields do not constrain malicious Python.

## Start with the example

Copy [examples/provider-plugin](../examples/provider-plugin), rename its package/ID, and install it in Accension's environment:

```sh
python -m pip install -e ./examples/provider-plugin
```

Enable it explicitly in trusted local configuration:

```yaml
plugins:
  enabled: [example-loopback]
providers:
  example:
    kind: example-loopback
    endpoint: http://127.0.0.1:9000/v1
    local: true
    auth: none
    protocol: openai_chat
    model_ids: [your-actual-loaded-model]
```

Top-level sections replace earlier sections: preserve your other providers when editing YAML.

## Contract

Subclass `local_ai_router.provider_sdk.ProviderPlugin` with `plugin_api_version = "1"`. Implement `manifest()` and `discover_models(context)`. Optional overrides: `auth_schema()`, `pricing()`, `health()`, `probe_model()` and `create_transport(protocol)`.

ProviderManifest declares protocols, auth, form fields, locality and inventory/pricing/health support. ProviderContext exposes the configured provider, shared HTTP client, auth and conservative `descriptor(remote_id, ...)` creation.

Return InventoryResult with complete=true only for a complete deployment inventory. Catalog membership is not entitlement. Preserve exact remote IDs, provenance and timestamps; unknown capabilities/cloud prices stay unknown.

Reuse an existing transport. A custom adapter implements async `generate(context, model, messages, maximum, schema=None, **kwargs)` and returns the built-in Generation/Usage contract. Inspect `transports.py` and `schema.py` before adding protocols.

Preserve central privacy, reservations, endpoint validation, error normalization and secret handling. Never follow untrusted pagination links with credentials. The shared client disables redirects and proxy inheritance. Keep requests and output bounded.

## Conformance

```sh
router provider test example
router provider test example --inference --budget 0
python -m pytest -q examples/provider-plugin/tests
```

Metadata checks validate manifest, credential schema, health and inventory. Opt-in generation tests cover one model and claimed JSON/stream/tool support. Cloud calls require `--allow-paid`. Also add offline fixtures for ID mapping, usage, authentication failures and secret-safe errors; use `tests/test_provider_conformance.py` as a reference.

API version changes must be deliberate. Plugins cannot replace built-in IDs, and community profiles cannot enable executable extensions.
