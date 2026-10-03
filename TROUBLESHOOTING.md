# Troubleshooting

Run `rtk proxy .\scripts\router.cmd doctor` first. Exit success means the diagnostic completed; inspect unavailable services in its JSON. It does not mean every provider is usable.

| Symptom | Cause and next action |
|---|---|
| No eligible planner/worker | Check provider enabled state, capability flags, prices, family quality priors, repository cloud permission and health circuit. Do not lower quality gates merely to force a route. |
| Microsoft says no access | The app/device-code flow may lack tenant permission. Browser portal sign-in does not supply application credentials. Use a tenant-approved application/authentication method; do not repeatedly retry the denied flow. |
| Foundry inventory login required | Configure authorized ARM credentials; inference API keys alone do not authorize ARM deployment listing. |
| Future deployment listed but ineligible | Supply verified prices/capabilities or fleet defaults; discovery deliberately avoids inventing them. |
| Qwen unavailable | Check whether your configured local model endpoint is running. Start its existing launcher when hardware/RAM policy permits; the router does not download or load it. |
| Critical task requires Jev | Configure an actual arbiter deployment or intentionally revise policy after reviewing the risk. No arbiter is configured by default. |
| Laya timeout | First load or CPU inference exceeded the 2-second routing deadline. The long-lived server keeps its child warm; deterministic fallback remains active. |
| Repository not registered | Add the exact root and independent validation commands in repositories.yaml, then restart the server/MCP client. |
| Stale plan | A file changed since planning. Generate a new plan; do not reuse the previous edit hashes. |
| Budget exceeded | Inspect `costs` and `trace`; failed calls may retain conservative reservations. Session totals persist until a new session ID is used. |
| MCP child tests hang on Windows | Child processes require isolated stdin and `CREATE_NO_WINDOW`. Covered by the full stdio regression test. |
| Port occupied | Check `status` before starting. Stop only this service through authenticated `router stop`; do not kill unrelated processes. |
| Client cannot see MCP tools | Restart the client and check its actual user configuration outside sandbox-redirected homes. |

Lifecycle commands:

```powershell
rtk proxy powershell -NoProfile -File .\scripts\start-router.ps1
rtk proxy .\scripts\router.cmd stop
rtk proxy .\scripts\router.cmd status
rtk proxy .\scripts\router.cmd trace REQUEST_ID
```

The Windows helpers currently target the installed default port 8765. If you change the port, update the profile/helper URLs and reinstall autostart configuration. Start scripts use hidden windows. Server logs are `.router/server.stdout.log` and `.router/server.stderr.log`; request metadata lives in SQLite.

If interrupted during file replacement, inspect the repository's `.router/backups/<request-id>/journal.json` and plan. V1 refuses to resume a partially executed plan automatically. Preserve later user edits when restoring. Normal handled failures perform checked rollback.
