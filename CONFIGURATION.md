# Configuration

UI and CLI share typed Settings and Management. Saves update ignored `config/local.yaml` with validation, backup, stale-file checks and atomic replacement. Active inference/repository operations prevent configuration replacement. Restart other processes after a change.

## Home and precedence

`--home PATH` overrides `ROUTER_HOME`. Editable installs default to the checkout; wheel/tool installs default to `%APPDATA%/accension` on Windows or `~/.config/accension` elsewhere.

Files load in order: version, providers, models, routing, budgets, cache, skills, repositories, discovery, roles, control_plane, local. Each is `config/NAME.yaml`. **Top-level sections replace rather than deep-merge.** A local providers section must preserve all providers you want.

Dynamic discovery, evidence and health live in `.router/router.sqlite3`, without continuously rewriting YAML.

## Example policy

```yaml
schema_version: 2
control_plane:
  routing_location: local-only
  fully_local: false
  telemetry: false
roles:
  classifier:
    strategy: auto
    locality: local-only
  arbiter:
    strategy: auto
    locality: local-only
  executor:
    strategy: auto
    locality: local-preferred
repositories:
  - path: /path/to/project
    privacy:
      mode: LOCAL_ONLY
      never_send: [secrets/**]
    validation:
      tests: ['{python}', '-m', 'pytest', '-q']
```

Local-only control still permits cloud execution for repositories explicitly marked CLOUD_ALLOWED or CLOUD_REDACTED. Fully Local blocks all built-in cloud use.

## CLI connection

```sh
router provider add local --kind openai-compatible --endpoint http://127.0.0.1:8080/v1 --field local=true --field auth=none
router provider add cloud --kind openrouter --field api_key_env=OPENROUTER_API_KEY
router provider test local
router role set executor --strategy auto --locality local-preferred
router config validate
```

Use `--api-key` for a hidden interactive prompt stored in the vault. Never put raw keys on command lines. The UI provides the same controls. Supply exact `model_ids` when inventory cannot enumerate account deployments.

## Profiles

```sh
router config export --file profile.json
router config import --file profile.json
router config migrate
```

Profiles contain routing, roles and budgets with private identifiers removed. They cannot execute commands, enable plugins, install software, reference credentials or weaken privacy. Trusted local config is separate from community profiles.

See [migration](MIGRATION_V1_V2.md), [providers](PROVIDERS.md) and [routing](ROUTING_POLICY.md). Full field definitions are in `src/local_ai_router/config.py` and `schema.py`.

## Runtime scale and savings

Use ignored config/local.yaml for additional sections. Runtime defaults: discovery_concurrency 8, health_probe_concurrency 8, inference_concurrency 8, calibration_concurrency 2 and discovery_timeout 120 seconds. These bound concurrent operations, not registry counts. Provider groups/include_models/exclude_models and role allowed_provider_groups narrow eligibility.

control_plane.mode accepts companion or sovereign. Savings defaults to enabled with DIRECT_MODEL and no selected baseline. Select savings.baseline_model explicitly; absent baseline means unavailable savings. Header periods: current, session, today, 7d, 30d and all. show_tokens/show_percentage affect presentation. Historical prices stay frozen; repricing is separate. [Savings guide](docs/SAVINGS.md).

Global/repository privacy accepts max_cloud_context_tokens_per_request and max_cloud_files_per_request. Nodes can impose stricter contracts; limits intersect without granting broader access. [Egress guide](docs/EGRESS_BUDGETS.md).
