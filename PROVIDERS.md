# Providers and automatic deployment discovery

Adapters exist for OpenAI-compatible Chat Completions, Responses, Azure Foundry OpenAI endpoints, Anthropic Messages, Ollama-compatible HTTP, local stdio MCP and deterministic mocks. `deployment_name` is the actual endpoint deployment ID. Public product names are not silently assumed to exist.

`config/discovery.yaml` polls configured inventories every 300 seconds while gateway/MCP runs. `router discover` refreshes immediately. Successful Azure ARM inventory adds future deployments and disables vanished automatically owned aliases. Failed inventory preserves the last registry. Inventory ownership persists independently of the optional cache.

Azure inventory needs your own subscription, resource group, resource name and authorized ARM credentials. Configure these in ignored `config/local.yaml`. A browser portal session is not an application bearer credential. All shipped cloud providers start disabled.

Discovery accepts an `AZURE_MANAGEMENT_TOKEN`, an explicitly configured renewable `token_command`, or the optional Azure Identity encrypted cache. These authorize ARM inventory, separately from inference authentication. The [Azure deployment-list API](https://learn.microsoft.com/en-us/rest/api/aiservices/accountmanagement/deployments/list?view=rest-aiservices-accountmanagement-2024-10-01) supplies actual deployment IDs; do not use a public model catalog as inventory.

## Enable a provider

1. Configure an available endpoint and authentication in `config/providers.yaml` or ignored `config/local.yaml`. Use environment variable names or Windows DPAPI enrollment; never put keys in the registry.
2. Set verified prices, capabilities and quality priors in `models.yaml` or `discovery.overrides`. Existing configured aliases take precedence over automatic duplicates.
3. Enable the provider, allow cloud for the intended repository if needed, restart the router, and run `doctor` and `discover`.

Example override structure, with values supplied from the deployment's current contract/pricing:

```yaml
discovery:
  # Include the other discovery fields when overriding this section in local.yaml.
  overrides:
    foundry:actual-deployment-id:
      tier: 2
      input_price: null
      output_price: null
      supports_responses_api: true
      supports_tools: true
      quality_priors: {default: 0.85}
```

`null` intentionally keeps this example ineligible. Future IDs are automatically accommodated by the adapters and registry. Automatic discovery does not invent prices or proof that a deployment meets a quality SLO. Use discovery defaults only for a fleet with known shared settings. Updates to YAML sections replace that section rather than deeply merging it.

Prompt cache: OpenAI/Foundry automatic prefix caching is accounted from usage when returned. Anthropic explicit caching is opt-in and needs both read/write prices; use the highest write price for the TTLs you permit. See [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). Capability flags must match the endpoint, not just a related model family.

The native gateway requires the corresponding protocol: `/v1/responses`, `/v1/chat/completions` or `/v1/messages`. It preserves native SSE/tool payloads, caps outputs and fails over before response bytes are sent. It does not convert protocols. Send explicit conversation history; server-side conversation references are rejected because hidden input cannot be bounded. Multimodal and remote-file input are rejected in V1 because their costs are not bounded by text token estimates; use text and client-executed function tools.

The example local provider points to `http://127.0.0.1:8080/v1` and starts disabled. Set the endpoint and actual deployment ID for your own service. The router does not load model weights or assume available GPU memory. No model weights are downloaded during setup.

## Use other local models

Qwen and Laya are optional. Provider names and model IDs are arbitrary; the selected protocol is what matters. Start with [config/examples/local-models.yaml](config/examples/local-models.yaml), copy it to ignored `config/local.yaml`, and set your model server's endpoint. That example replaces the provider, model, discovery and routing sections, so retain other configured entries when combining setups.

1. Run a local server exposing OpenAI-compatible `/v1/models` and `/v1/chat/completions` and point `endpoint` to its `/v1` base. Multiple local servers can have different provider names and ports. The `ollama` adapter also uses these OpenAI-compatible routes, not Ollama's native `/api` routes.
2. Add provider names to `discovery.inventory_providers` to discover future model IDs automatically. If the endpoint does not offer a deployment inventory, explicitly add `models` entries with `id`, `provider`, and the actual `deployment_name` instead.
3. Set context size, output limits, concurrency, supported capabilities and measured quality priors for each model. `discovery.overrides` accepts `provider:deployment-id` keys. The conservative example prior permits simple explanations but does not satisfy the default coding SLO. A newly discovered model is not proof of coding quality.
4. Restart the service after changing YAML, then run `router config validate`, `router doctor` and `router discover`. Inventory changes at already configured servers are refreshed automatically without restarting.

For a static local model, this is the shape of an entry under `models`:

```yaml
models:
  - id: my-coder
    provider: my-local-server
    deployment_name: replace-with-real-server-model-id
    input_price: 0
    output_price: 0
    context_window: 32768
    max_output: 4096
    concurrency_limit: 1
    quality_priors: {default: 0.85}
```

These limits are examples; use the actual server settings. Zero prices mean no per-token API charge for a local model, not zero hardware or electricity cost. Capability flags default conservatively. Only enable tools, structured output, streaming or Responses when that model and server support them. Set a configured model ID in `routing.jev_model` to use it as the JSON routing arbiter; that role does not require a model literally named Jev. A stdio code provider can also use `kind: mcp` with configurable `command`, `args` and `tool`; it must accept the `task_id`, `instruction`, `context`, `max_output_tokens` contract implemented in `providers.py`.

## Optional local classifier

With `routing.laya_enabled: false`, classification uses the built-in deterministic policy. To enable Laya, set `laya_command` to its executable and `laya_args` to its server arguments. These settings keep their existing names for compatibility, but can launch another stdio MCP server exposing the same decision contract. `classifier_tool` selects the MCP tool name and defaults to `laya_decide`.

The classifier receives `state: {request: ...}`, `min_confidence`, and a JSON `schema`. It must return a text JSON object containing `values` (task family, complexity, risk and planning requirement) and per-field numeric `confidence`. Arbitrary classifier protocols need an adapter; changing the tool name alone does not translate protocols. Timeout, malformed output or low confidence falls back to deterministic routing. A classifier cannot downgrade deterministic critical risk.

```yaml
routing:
  laya_enabled: true
  laya_command: /path/to/classifier/python
  laya_args: [/path/to/classifier/server.py]
  classifier_tool: my_decision_tool
  laya_timeout: 2
  laya_confidence_threshold: 0.80
```

Personal skill paths are likewise opt-in through `skills` in `config/local.yaml`. No workstation paths or model binaries are shipped.
