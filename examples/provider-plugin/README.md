# Example provider package

An installable API version 1 plugin. It uses explicitly configured model IDs
and reuses Accension's OpenAI Chat transport. It does not start a model server,
download weights or pretend configured IDs prove availability.

Install into the same Python environment as Accension:

```sh
python -m pip install -e ./examples/provider-plugin
python -m pytest -q examples/provider-plugin/tests
```

Enable `example-loopback` explicitly and configure a running loopback endpoint.
See [the SDK guide](../../docs/PLUGIN_SDK.md) for configuration and conformance.
Only install plugins you trust: their Python code runs without a sandbox.
