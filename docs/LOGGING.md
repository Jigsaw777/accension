# Troubleshooting logs

Accension keeps local, redacted logs so you can see why something failed. Task traces explain execution steps; logs also cover command, provider and gateway failures.

```sh
accs doctor
accs logs
accs logs tail --follow
accs logs show REQUEST_OR_ERROR_ID
accs logs --level ERROR --component provider
accs logs --run RUN_ID --since 2026-01-01
accs logs path
accs logs export --output diagnostics.zip
accs doctor --mock --bundle diagnostics.zip
```

Logs live in `ROUTER_HOME/logs/accension.jsonl`. Without `--home` or `ROUTER_HOME`, the home is your user configuration folder (`%APPDATA%/accension` on Windows or `~/.config/accension` elsewhere). An explicitly chosen checkout home still stores its logs there; those files are ignored by Git and rejected by the public scan.

Each log rotates at 10 MB and keeps five backups. The local settings `log_max_bytes`, `log_backups` and `log_level` control this. The default level is INFO. `--debug` enables DEBUG frame metadata without printing source lines, local variables or exception bodies. Logs are best-effort: disk or permission failures print a safe warning and do not stop a model call or repository operation.

Each event includes a timestamp, level, component and event name. Request, run, plan, task, session and error IDs link related events where available. Provider events include model, duration and token counts. Gateway logs use route templates, not query strings or arbitrary URLs. CLI logs record the command name and exit code, not its argument text.

Logs discard prompts, source, skill text, provider bodies and unknown payload fields. Central redaction covers common credential patterns, authentication headers, passwords, cookies, vault fields, signed URL queries and personal paths. Debug output does not disable these protections. Redaction is a safeguard, not a guarantee for every possible secret format.

Open **Logs** in the UI to filter events, copy an ID or open its task trace. A diagnostic export contains redacted event metadata, OS/Python/version details, configuration counts and cached provider health. `doctor --bundle` adds a bounded check summary. Log export makes no provider calls. It excludes vaults, tokens, configuration files, databases, repository source and skill files. Review the bundle before sharing it.
