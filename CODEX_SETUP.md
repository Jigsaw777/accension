# Codex setup

The installer adds `mcp_servers.local-ai-router` to the existing user `~/.codex/config.toml`, preserving other entries. It appends concise routing guidance to `~/.codex/AGENTS.md`; existing personal preferences are preserved. Restart/reload Codex to expose newly registered tools. This does not change the active host model.

The optional CLI gateway profile is `~/.codex/local-ai-router.config.toml`. Installed Codex 0.159.0-alpha.12.1 uses a separate profile file with top-level settings. This matches the [current Codex profile documentation](https://learn.chatgpt.com/docs/config-file/config-advanced); do not copy legacy `[profiles.name]` examples here.

```powershell
rtk proxy .\scripts\codex-router.cmd
rtk proxy .\scripts\codex-direct.cmd
rtk proxy codex --profile local-ai-router mcp get local-ai-router
```

The router helper reads the local token into its child environment and selects the profile. The direct helper uses the default configuration. Existing login and normal defaults are preserved. The profile uses Responses at `http://127.0.0.1:8765/v1`; an eligible Responses deployment with appropriate tool support is required for live use.

Check the registration with your installed Codex CLI. Live routed conversations require an eligible configured provider; the bundled tests cover mock protocol behavior. MCP orchestration is the recommended integration for this V1; native gateway conversation-state limitations are in PROVIDERS.md.

`scripts/install_clients.py` previews additions; `--apply` backs up exact prior bytes and applies user-level changes. Backups and their manifest are under `.router/integration-backups/`. To uninstall, first stop the router, then pass the installation manifest to `scripts/uninstall_clients.py`. It restores only files still matching installed hashes; later user edits are not overwritten. When moving the router, recreate its virtual environment and run `scripts/install_clients.py --replace-from OLD_ROOT --apply`. Only matching managed registrations are migrated, with backups.
