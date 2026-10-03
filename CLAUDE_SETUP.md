# Claude setup

The installer preserves existing servers and preferences while adding stdio `local-ai-router` to `%APPDATA%/Claude/claude_desktop_config.json` and user `~/.claude.json` for Claude Code. It appends routing guidance to `~/.claude/CLAUDE.md`. Claude Desktop instructions are also supplied in `docs/CLIENT_INSTRUCTIONS.md`; add them to the relevant project instructions when appropriate.

Restart Claude Desktop after the configuration change. Its MCP server delegates repository work; it does not replace Claude Desktop's underlying model transport. Use Claude Code `mcp get local-ai-router` to verify connectivity.

```powershell
rtk proxy claude mcp get local-ai-router
rtk proxy .\scripts\claude-router.cmd
rtk proxy .\scripts\claude-direct.cmd
```

`claude-router` sets the supported `ANTHROPIC_BASE_URL`, local auth token and virtual model aliases in a child PowerShell environment. It requires an enabled Anthropic Messages-compatible provider. Foundry OpenAI deployments are not automatically Messages-compatible. See [Claude Code LLM gateway documentation](https://code.claude.com/docs/en/llm-gateway).

`claude-direct` clears the router-specific gateway variables for its child process and runs normal Claude Code. Parent environment and default client configuration remain intact. The live gateway profile was generated but not exercised against a paid Anthropic deployment. MCP protocol and full orchestration are covered by local mock tests.

External configuration backups use the same installation manifest described in CODEX_SETUP.md. The uninstall script refuses to overwrite configuration changed since installation.
