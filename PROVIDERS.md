# Providers and evidence

A provider is a local model server or cloud AI service. Add one through **Providers** in the app or `accs provider add`. Keys are stored as credential references and are never shown again. A discovered model still needs suitable capabilities, access and pricing before Accension can use it.

| Kind | Protocol | Authentication / inventory |
|---|---|---|
| openai | Chat, Responses | API key; models |
| anthropic | Messages | API key; models |
| foundry | Responses, Chat, Messages | Key, Azure Identity or token command; Azure deployment inventory / explicit IDs |
| bedrock | Converse | AWS credential chain and optional profile; regional catalog |
| vertex | Gemini | ADC or explicit service-account file; exact configured IDs |
| gemini | Gemini | API key; paginated models |
| openrouter | Chat | Key; catalog, supported parameters and token prices |
| groq, together, fireworks, mistral, cerebras, huggingface | Chat | Key; compatible inventory |
| openai-compatible, anthropic-compatible | Matching native protocol | Custom endpoint and optional key |
| ollama | Chat | Native tags and loaded-model inventory |
| lmstudio, vllm, llamacpp | OpenAI-compatible | Loopback inventory |
| mcp | Stdio MCP | Trusted command/tool and explicit model IDs |

An implemented adapter with mock tests is not a claim that every vendor/model/version has been live certified. Only enable capabilities your deployment supports.

## Native credentials

Optional extras are `accension[aws]`, `accension[google]`, `accension[azure]`. AWS uses boto3 sessions, bounded timeouts and disabled SDK inference retries; SSO/session refresh follows the SDK. Bedrock bearer credentials, where supported by that SDK, use its native configuration rather than the generic vault key field. Azure inventory requires management-plane permission in addition to inference access. Vertex needs project, location and actual model IDs.

## Local discovery

Short startup probes visit configured loopback endpoints only: Ollama 11434, LM Studio 1234, compatible server 8080 and vLLM 8000 by default. No LAN scan or automatic model download occurs. Empty/missing runtimes leave configuration mode usable. Add custom endpoints explicitly.

Local servers are trusted and can themselves forward requests. Resource routing uses available RAM, reported loaded status and configured estimates; it is not a GPU scheduler.

## Inventory, pricing and probes

Models retain deployment identity, protocols, locality, capabilities, context, quality, resource estimates, provenance and timestamps. Failed inventory retains stale cached records. Only complete inventories mark missing models removed; history survives.

Evidence priority is user override, active probe, provider metadata, conservative defaults. Unknown/stale cloud prices block automatic budgeted routing unless estimates are explicitly allowed. Local API token price is zero, without claiming free hardware/electricity. Non-token vendor charges are not modeled.

```sh
router provider test NAME
router provider test NAME --inference --budget 0.01
router calibrate preview --model PROVIDER:MODEL --budget 0.05
router calibrate run --quote-id QUOTE_ID
```

Default provider tests are metadata-only. Inference tests use one model; cloud tests and calibration additionally require `--allow-paid`. Quotes expire after 15 minutes, are single-use, and become stale after config changes. Tiny capability evidence is cached for 24 hours.

Native stream events, returned tool-call structures and generated test images underpin relevant probes. Tool calls are never executed. Converse structured generation works, but optional native Bedrock streaming/tool/vision/reasoning probes currently return unsupported.

## Gateway

Native `/v1/responses`, `/v1/chat/completions`, `/v1/messages` and model-list endpoints preserve matching OpenAI/Anthropic payloads/tools/SSE. They do not translate arbitrary clients into Bedrock or Gemini; use MCP orchestration for those providers. Stateful response continuation cannot be safely switched between providers; unsupported virtual-routing combinations fail clearly.

Protocol references: [OpenAI Responses](https://platform.openai.com/docs/api-reference/responses), [Anthropic Messages](https://docs.anthropic.com/en/api/messages), [Bedrock Converse](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-runtime/client/converse.html), [Gemini API](https://ai.google.dev/api/generate-content), [Vertex authentication](https://cloud.google.com/vertex-ai/docs/authentication), [Azure OpenAI](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/reference), [OpenRouter models](https://openrouter.ai/docs/api-reference/list-available-models).

## Scale and learned evidence

No fixed provider/model count cap is imposed. Use `accs provider add KIND --name INSTANCE` to distinguish accounts, regions and endpoints; model identity includes that instance. Discovery/search are paged and concurrent operations are bounded. Memory, storage and upstream quotas remain practical limits. Repeated --group, --include-model and --exclude-model flags constrain inventory; roles can allow selected provider groups. Inventory discovery makes no paid probes. Inspect learned uncertainty with `accs model dna MODEL_ID`.
