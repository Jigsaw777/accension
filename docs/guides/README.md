# Accension guides

Start with [Windows desktop](windows.md), follow [your first Workbench project](workbench.md), and use the [architecture and source map](architecture.md) when contributing. The [screenshot gallery](screenshots/README.md) shows the local and paired-device layouts in both themes.

These guides describe the checked-in application, package version 1.1.0. Application versions, database migrations and local implementation-prompt versions are separate. Screenshots contain synthetic examples; they are not live customer data or proof of provider quality or savings. The [current feature screenshots](screenshots/feature-showcase.md) accompany the [README walkthrough](../../README.md#workbench).

| Goal | Read |
|---|---|
| Install, open, update or uninstall Windows desktop | [Windows tutorial](windows.md) |
| Create a project, define work, execute and review | [Workbench tutorial and reference](workbench.md) |
| Understand ownership, interfaces and safety | [Architecture](architecture.md), [core design](../../ARCHITECTURE.md) |
| Find modules and choose where to change code | [Source map](architecture.md#source-map) |
| Configure routing, providers and clients | [UI](../../UI.md), [providers](../../PROVIDERS.md), [configuration](../../CONFIGURATION.md), [MCP](../../MCP.md) |

Local implementation prompts and task cards are ignored by Git. They are not end-user documentation or runtime prompts. The packaged role prompts remain under `src/local_ai_router/data/prompts`; optional home-directory overrides stay private.
