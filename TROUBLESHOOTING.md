# Troubleshooting

Start with `router doctor` and System. Doctor does not invoke paid inference. Keep diagnostics private if they contain local paths or model identifiers.

| Symptom | Next action |
|---|---|
| No models / no eligible route | Connect/discover; inspect capability, quality, context, privacy and price rejection reasons |
| Missing local runtime / empty inventory | Start your runtime and load a model explicitly; deterministic decisions still work |
| AUTH_REQUIRED | Reconnect or refresh the native CLI/ADC/SSO session |
| Unknown / stale price | Enter current prices, refresh metadata or explicitly permit estimates |
| Unavailable pin | Inspect fallback/health and update renamed deployment IDs |
| 429 / provider outage | Respect cooldown/Retry-After; eligible alternatives remain budget-bound |
| Offline | Cached registry and local providers work; cloud-dependent work fails clearly |
| OS vault unavailable | Unlock Keychain/Secret Service, install the vault extra or use an environment reference |
| Port occupied | Try `router ui --port 8767`; check downstream recursion |
| Session expired / CSRF failure | Reload /ui to bootstrap a new same-origin session; no code is needed |
| Config changed elsewhere | Reload before saving; restart other processes after changes |
| Database busy | Finish competing operations and retry; do not delete the database |
| Integrity failure | Stop writers and inspect a backup; corrupted state is not silently replaced |
| Context cannot fit | Reduce/split the task or select a larger eligible context |
| Missing validation | Register trusted test/build/lint commands |
| Plugin unavailable | Check entry point name, API version 1 and explicit plugins.enabled |

## Interrupted work

```sh
router recovery list
router recovery inspect PLAN_ID
router recovery resume PLAN_ID
router recovery rollback PLAN_ID
router recovery discard PLAN_ID
```

Choose one action after inspection. Resume needs an exact checkpoint. Incomplete batches require rollback and replanning. Rollback preserves later user edits. Discard leaves files/backups intact. Uncertain upstream charges stay reserved.

Bug reports should include version, OS/Python, minimal non-secret config, command and normalized error. State whether inference was live or mocked. Never attach a full private database, token, client config or source-bearing rollback directory.

## Accounting and integrations

Unavailable savings: select a registered cloud baseline and inspect missing price/usage or receipt accounting gaps. Negative savings are retained when retries/control overhead or the selected route exceed the estimated baseline. The costs ledger retains conservative reservations and may differ from reported-usage analytics.

Use accs launch CLIENT --dry-run to inspect installed transport capabilities. Unsupported desktop surfaces fall back to Companion MCP. Resume is limited to unfinished valid checkpoints; finalized runs require a fresh plan.
