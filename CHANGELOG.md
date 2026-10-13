# Changelog

## 1.1.0

### Added

Persistent Workbench projects, structured PRDs, reviewed planning/decomposition, dependent work items, configured agents, approved feature delivery, recorded attempts and evidence-bound completion review. Activity, model bindings, agent metrics and opt-in paired HTTPS LAN access share the existing Engine and SQLite store.

### Improved

One application for model routing and Workbench, with shared branding, themes and responsive layouts. Bounded board/history/metrics reads, current-source Windows installers with bundled Python and source/asset/checksum manifests, and screenshot-supported tutorials.

### Security

Scoped device grants, TLS/Origin/CSRF checks, frozen execution contracts and independent host validation. Repository matching avoids filesystem access for unregistered roots; request classification avoids backtracking; delivery ticks serialize on the shared database owner.

### Compatibility

CLI aliases `accs`, `accension` and `router`, module `local_ai_router`, and MCP name `local-ai-router` remain supported. Product 1.1.0, database schema 9, configuration schema 2 and AXIR schema 1 are distinct.

### Upgrade notes

Stop active work and back up the profile. Migrations create verified backups. Older binaries must not open upgraded state; restoration loses subsequent database changes and does not undo repository edits. See [upgrade notes](docs/UPGRADE_NOTES.md).

### Known limitations

Windows downloads are unsigned branch prereleases, not a stable numbered release. Mock screenshots/demos are not benchmarks; estimates are not invoices. Paid provider combinations and physical-device CA onboarding need validation in the intended environment. Workbench does not automatically commit, push or merge. [Release notes](docs/RELEASE_NOTES.md).

## 1.0.0 — release candidate

First public release of Accension. Runs locally and connects local or cloud AI providers. Includes the `accs` CLI, local UI, Companion and Sovereign modes, provider extensions, portable AXIR plans, Model DNA, verified repository execution, recovery, execution receipts and Savings Pulse.

Adds local skill discovery, trusted text skills, offline search, ordered composition, unlimited saved presets and recipe outcome tracking. Adds redacted rotating logs with trace/error IDs and diagnostic exports.

Protects contributions with owner review, required tests and security checks, secret/dependency scanning and hardened release artifacts. Public release publication remains pending maintainer approval. Internal configuration and database schema versions are independent of this product version.
