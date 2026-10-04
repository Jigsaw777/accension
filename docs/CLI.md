# ACCS command reference

`accs`, `accension` and `router` share one parser and engine. Bare invocation opens the UI. Use `accs COMMAND --help` for exact flags. Human output is default; `--json` produces machine-readable stdout. Progress/errors use stderr. `--no-color` is accepted (output is already plain); `--debug` prints a redacted diagnostic traceback. `--home PATH` overrides `ROUTER_HOME`; `--mock` uses deterministic fixtures.

## Skills, presets and logs

Skills are reusable instruction files. Presets save groups of skills. Logs help investigate failures without storing full prompts or source code.

```sh
accs skill scan
accs skill add ./review/SKILL.md
accs skill info review
accs skill trust review
accs skill search debugging
accs skill compose "Fix the cache" --learned
accs preset create focus --skill review
accs preset use focus --repo .
accs run "Fix the cache" --repo . --preset focus
accs skill recipes
accs logs --level ERROR
accs logs show ERROR_ID
accs logs export --output diagnostics.zip
```

See [all skill and preset operations](SKILLS.md) and [log filters and rotation](LOGGING.md). `--skill` adds a temporary skill; `--skill-mode auto` allows local suggestions. Presets have no count limit. Invalid or oversized active combinations fail with an explanation.

## Setup and service

```sh
accs init
accs init --non-interactive --mode sovereign --preset fully-local --repo . --validation python-unittest
accs doctor
accs ui --no-browser
accs start --background
accs status --json
accs restart
accs stop
accs serve --port 8765
accs version
```

Setup is terminal-native. Keys use a hidden prompt and OS vault; noninteractive setup uses credential references. The background service needs no administrator rights and installs no autostart. Remote service mode is refused. Default hub/UI/task API/gateway port: 8765; legacy enrollment: 8766.

## Providers, models and roles

```sh
accs provider list
accs provider add ollama
accs provider add openrouter --name team-cloud --credential-env OPENROUTER_API_KEY --non-interactive
accs provider add custom --name local-server --endpoint http://127.0.0.1:8080/v1 --protocol openai --local --non-interactive
accs provider test team-cloud
accs provider refresh team-cloud
accs provider remove team-cloud
accs model list --provider local-server --offset 0 --limit 50
accs model search coder
accs model info PROVIDER:MODEL
accs model dna PROVIDER:MODEL
accs model dna PROVIDER:MODEL --reset
accs model probe PROVIDER:MODEL --capability text --budget 0.01 --allow-paid
accs model enable PROVIDER:MODEL
accs model disable PROVIDER:MODEL
accs model refresh --provider local-server
accs role list
accs role explain executor
accs role set executor PROVIDER:MODEL
accs role auto executor
accs role reset executor
```

Provider add also accepts `--kind`, `--auth`, `--region`, `--profile`, `--project`, repeated `--field KEY=JSON`, `--group`, `--include-model` and `--exclude-model`. `--api-key` opens a hidden prompt. Provider test is metadata/health by default; inference requires `--inference`, budget and paid opt-in for cloud. Follow model-list `next_offset` until absent; there is no first-page registry limit. `models` remains the old list alias.

## Repository execution

```sh
accs repo list
accs repo add . --privacy LOCAL_ONLY --check 'tests=["{python}","-m","unittest","discover","-v"]'
accs route "Refactor authentication" --repo . --explain
accs plan "Refactor authentication" --repo . --export auth.axir.json
accs inspect auth.axir.json
accs reroute auth.axir.json
accs plan diff old.axir.json new.axir.json
accs execute auth.axir.json --repo .
accs run "Implement a greeting feature" --repo . --budget 0.25 --local-only
accs run "Plan an API change" --repo . --mode plan-only
accs resume RUN_ID
accs rollback RUN_ID
accs trace RUN_ID
accs receipt RUN_ID --markdown --output receipt.md
```

Run/plan accept repeated `--constraint`, `--session`, `--privacy`, `--local-only`, `--quality` (0–1), `--max-cloud-context` and `--budget`. `--dry-run` returns a route without inference. Route always explains its selection; `--explain` is an explicit intent flag. Reroute previews binding without changing the artifact. Execution requires a registered root, matching fingerprint and known validation names. Only unfinished recoverable runs can resume; rollback preserves later edits.

## Costs, evidence and maintenance

```sh
accs costs
accs savings --today
accs savings --7d
accs savings --30d
accs savings --all --json
accs savings --session SESSION_ID
accs savings --run RUN_ID --reprice
accs lab compare RUN_ID
accs calibrate preview --model PROVIDER:MODEL --budget 0.05
accs calibrate run --quote-id QUOTE_ID --allow-paid
accs calibrate --quick --local-only
accs cache stats
accs cache clear
accs config validate
accs config export --file profile.json
accs config import --file profile.json
accs config migrate
accs plugin list
accs plugin info openai-compatible
```

Costs is the conservative reservation ledger; savings is receipt-based decimal analytics. Uncertain calls can retain a reservation while actual cost is unknown. Repricing requires a run and returns separate analysis. Lab compares completed runs without shadow calls. Cache clear preserves receipts and savings.

## Integrations and completion

```sh
accs mode companion
accs integrate list
accs integrate codex --mode companion
accs integrate codex --mode companion --apply
accs integrate undo codex
accs launch codex --dry-run
accs launch claude
accs launch codex --direct
accs completion bash
accs completion zsh
accs completion fish
accs completion powershell
```

Completion prints a script to save/source in that shell. It completes command names, not every model ID. Launcher dry-run inspects installed capabilities without starting an agent. Put client option arguments after `--`.

Legacy commands: discover, models, integration preview/install, recovery list/inspect/resume/rollback/discard, profiles reset, eval, azure-login, enroll and demo.

## Exit status

| Code | Meaning |
|---|---|
| 0 | Completed |
| 2 | Invalid input/configuration or missing resource |
| 3 | Provider failure |
| 4 | Privacy or budget refusal |
| 5 | Execution/validation or unclassified failure |
| 6 | Service/network/OS failure; status reports stopped |
| 130 | User interruption |

Launched clients propagate their own nonzero status. With `--json`, runtime failures include error, type and exit_code. Unknown commands and argument syntax errors still print standard argparse usage/error text to stderr and exit with code 2.
