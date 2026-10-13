# Accension 1.1.0

Accension 1.1.0 adds a persistent Workbench alongside model routing. Both views share one local Engine, configuration, database, logo and theme.

- Organize projects, structured PRDs and dependent work items; generate and review planning proposals before import.
- Review agents, model policy, file scope, registered checks and spending before starting a feature. Completion requires the applicable evidence-bound review.
- Inspect recorded attempts, artifacts, receipts, activity and agent performance. Usage estimates and missing prices are identified explicitly.
- Keep live-load and first-attempt metrics queries bounded by existing indexes as attempt history grows.
- Match registered repositories before filesystem access and classify API/database requests without a backtracking expression.
- Serialize concurrent feature-delivery ticks on the shared SQLite connection.
- Install the Windows desktop package with bundled Python. Successful branch builds publish current-source installers with source, checksum and asset manifests.
- Follow the updated [README](../README.md), [Workbench tutorial](guides/workbench.md), [feature delivery guide](guides/feature-delivery.md), [architecture](guides/architecture.md) and [current screenshots](guides/screenshots/feature-showcase.md).

## What this changes for users

1.0.0 provided routing, local UI, portable plans and verified repository execution. 1.1.0 connects that Engine to persistent feature planning and delivery. Start a manual project without a model, then add reviewed requirements, dependent tasks and bounded execution as needed.

PRD approval accepts requirements; import creates records; execution approval permits the current scope; completion review accepts evidence. Assignment/model output cannot skip these boundaries. The [Workbench tutorial](guides/workbench.md) demonstrates each action.

## Install

Use the [Windows installer](guides/windows.md) or [Python source-checkout instructions](guides/quickstart.md). Published `windows-main` and `windows-develop` downloads are mutable branch prereleases. Their manifest identifies the source commit; 1.1.0 alone does not identify an artifact or stable GitHub tag. Installers are unsigned. The Python instructions do not assume PyPI publication.

Local state stays on the machine. Optional authorized cloud inference can transmit project context externally. LAN is disabled by default and requires HTTPS, pairing and workspace grants. [Privacy](../PRIVACY.md) · [Security](../SECURITY.md).

## Upgrade procedure

Stop active work before upgrading. Existing local state migrates to SQLite schema 9 with a verified backup. Older binaries must not open upgraded state; restoring a backup loses later database changes and does not undo repository edits. See [upgrade notes](UPGRADE_NOTES.md).

The CLI names `accs`, `accension` and `router`, Python module `local_ai_router`, and MCP name `local-ai-router` remain supported. Local implementation prompts and task notes remain Git-ignored.

## Evidence and limits

README screenshots use deterministic mock inference in an isolated repository with real local tests. They are not production benchmarks or measured provider savings. Paid providers and physical-device certificate onboarding need validation in the intended environment. Windows installers are unsigned; verify their manifest and checksum.
