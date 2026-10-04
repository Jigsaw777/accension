# Skills and presets

A skill is a reusable instruction file. Accension can find your local skills, group them into presets, and suggest a combination for a task. Skills are optional. Searching and composing skills work offline, without embeddings or paid model calls.

## Add and inspect a skill

```sh
accs skill scan
accs skill scan --directory ./my-skills
accs skill add ./my-skills/review/SKILL.md
accs skill list
accs skill info review
accs skill trust review
accs skill validate review
```

Scanning checks configured folders or recognized `.codex/skills`, `.agents/skills`, and `.claude/skills` folders in your user directory. It does not scan the whole computer. It reads only `SKILL.md` files, skips linked subdirectories, and stops at six directory levels or 10,000 visited directories. Scan a narrower folder if needed. Scripts beside a skill are never executed.

New files start untrusted. Inspect their text before trusting them. Use `--id NAME` when adding a file whose name conflicts with another skill. Existing explicit `skills: name: path` settings retain their previous trust semantics. Removing a skill only removes its registry entry; it keeps the original file.

```sh
accs skill disable review
accs skill enable review
accs skill untrust review
accs skill remove review
accs skill search "debugging"
accs skill suggest "Refactor a concurrency-safe cache"
accs skill compose "Refactor a concurrency-safe cache" --learned
```

Search matches names, descriptions, tags, task families and a local word index. Suggestions explain the words or family that matched. No cloud service receives this search.

## Save a preset

A preset saves a group of skills you often use together.

```sh
accs preset create focus --skill review
accs preset show focus
accs preset use focus
accs preset use focus --repo .
accs preset use focus --session work
accs preset clone focus focus-copy
accs preset update focus-copy --description "Review small changes"
accs preset rename focus-copy review-small
accs preset export focus --output focus.json
accs preset import shared-preset.json
accs preset list --offset 50 --limit 50
accs preset delete review-small
```

There is no preset-count limit. SQLite stores presets locally and list commands use pages. Imported presets refer to local skill IDs; they cannot import executable code or grant trust. Missing, disabled or untrusted skills block use of that preset.

For a task, the order is: explicit `--preset`, repository default, session default, then global default. `--skill` adds temporary skills to that selection. `--skill-mode off` uses no skills. `--skill-mode auto` asks the local composer to select skills when no preset or explicit skills were selected.

```sh
accs run "Fix this bug" --repo . --preset focus
accs run "Fix this bug" --repo . --skill review
accs run "Fix this bug" --repo . --skill-mode auto
```

The **Skills** page supports search, scan, inspect, add, enable, disable and trust. The **Presets** page supports creating, editing, duplicating, deleting and assigning presets. Playground offers optional suggestions you can accept, remove or ignore.

## Compatibility and context limits

Plain Markdown works. Optional YAML frontmatter can describe dependencies and ordering:

```yaml
---
name: review
description: Review small Python changes
tags: [python, review]
task_families: [coding]
roles: [reviewer]
requires: [planning]
after: [planning]
before: [summary]
conflicts_with: [skip-review]
---
Check the change against its acceptance criteria.
```

Only declared conflicts affect composition. Missing `requires` dependencies and dependency cycles block execution. Ties preserve the requested order. The composer skips incompatible suggestions and reports which ones it skipped; explicit selections fail with an explanation.

The default `active_skill_token_budget` is 4,000 estimated tokens across an active combination. A preset's `--token-budget` can lower that limit. Skill files have a separate 250 KB limit. Instructions are never silently truncated. Token estimates use UTF-8 byte size; they are not provider billing measurements.

Skills cannot change registered tools, repository privacy, user constraints, spending limits or the execution plan's restrictions. Secret-like contents are rejected. A plan records ordered skill IDs, hashes, preset ID and selection source. AXIR exports and execution receipts omit skill paths and full text. If a skill changes, execution stops until you inspect it, add/trust it again, and create a new plan.

## Skill Recipe Memory

Accension records local outcomes for each combination of skill hashes and task family. It counts verified successes, failures, repairs and escalations. A completed run counts as successful evidence only after final independent validation passes. One run is labelled limited evidence.

```sh
accs skill recipes
accs skill recipes --task-family coding
accs skill recipe show RECIPE_ID
accs skill recipe reset
```

With `--learned`, the composer may prefer a matching recipe after at least five samples and at least 70% observed success. Changed skill hashes do not inherit old evidence. These are correlations, not proof that a skill caused success. Names, outcomes and statistics stay in your local database. Optional local embeddings are not implemented in this release.
