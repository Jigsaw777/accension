# V1 hardening verification

Public version: **1.0.0**. This release candidate adds local skills, presets and troubleshooting logs, plus enforced GitHub review and security checks. The work stays on `codex/v1-hardening` for maintainer review. No release tag has been created.

## Live GitHub protection

Read back from GitHub on 2026-10-04 with `scripts/configure_github_security.py --verify`:

| Setting | Live result |
| --- | --- |
| main protected | YES |
| PR required | YES |
| CODEOWNER required | YES — rule enabled; see bootstrap limit below |
| Owner | @Jigsaw777 |
| Stale reviews dismissed | YES |
| Required Merge Gate | YES |
| Force pushes blocked | YES |
| Deletion blocked | YES |
| Conversation resolution | YES |
| Linear history / squash only | YES |
| Secret scanning / push protection | YES |
| Dependabot security updates | YES |
| Private vulnerability reporting | YES |
| Actions SHA pinning | YES |

`Protect main` requires one approval and Code Owner review. Admins can bypass the review rule **only through a PR**, solving the owner's self-review restriction. A separate rule has no bypass and requires an up-to-date branch, Merge Gate and protected history. Signed intermediate commits are recommended, not required. Automatic merge is disabled.

**Bootstrap limit:** `.github/CODEOWNERS` exists in this PR but is not yet on `main`. GitHub can resolve its ownership rule after the reviewed PR lands. This report does not claim that a missing default-branch file already enforces ownership.

## Required checks

The CI workflow requires Ruff lint and format, documentation checks, public-file scan, sensitive-call policy, Bandit, JavaScript syntax, nine OS/Python test combinations, Python and JavaScript CodeQL, dependency audit, PR dependency review, Gitleaks, zizmor and a clean wheel installation. Merge Gate fails when a required job fails, is cancelled or unexpectedly skips. PR dependency review is intentionally skipped for non-PR events.

Local Windows/Python 3.12 verification including the CodeQL regressions: **255 passed**, four expected configuration-migration warnings, **80.48% coverage**. The coverage floor is 80% across the package, with no custom coverage exclusions. The contributor check command passed Ruff, Bandit, zizmor, documentation/public-file checks, sensitive-call review and the complete test suite. Clean-wheel installation passed before the security follow-up; it was not repeated for these source-only changes. The final GitHub matrix is recorded in [PR #1](https://github.com/Jigsaw777/accension/pull/1/checks); do not infer its current outcome from this file.

The security follow-up fixes a gate that missed severity metadata stored in SARIF query-pack extensions. Replaying the original CodeQL report now blocks all eight high-severity findings, with or without explicit result levels; unresolved metadata also fails the gate. Private-key detection uses bounded headers and redaction scans each block once, including incomplete blocks. Execution and skill defaults use the registered repository's canonical path. Validation reads and sanitizes output through its original file handle instead of reopening a pathname after repository tests run. No CodeQL queries were disabled or alerts dismissed. Fresh CodeQL analysis remains for maintainer CI review.

Tests use fixtures and mock HTTP providers. Unexpected external sockets are blocked in the test process. No paid AI calls were used. Browser checks covered the Skills page, preset creation and log filtering; the test browser reported no console errors.

## Secret audit

A fresh Gitleaks scan checked all reachable local Git history after fetching origin and tags. It found no confirmed secrets. History was not rewritten. Reports contain only rule category, path and commit metadata; they never print matched values. The working-tree scan also checks tracked and prospective public files. Final commit history is scanned again in CI.

## Skills and presets

The SQLite registry records local skill metadata, trust, enablement, hashes, token estimates and declared compatibility. Scans stay within configured or known skill folders, with bounded depth and file size. Skills are text; adjacent scripts never run. Legacy configured skills remain supported.

`accs skill` supports scan, add, list, inspect, search, trust, enable, validate, suggest, compose and recipe inspection/reset. `accs preset` supports create, list, show, update, clone, rename, use, import, export and delete. Presets have no count limit; lists are paged. The UI has Skills and Presets pages and optional task suggestions in Playground. MCP provides grouped skill/preset operations.

Suggestions use local word matching and task-family labels. Dependencies and declared before/after rules determine ordering; declared conflicts and cycles are rejected. Combined instructions must fit the active token budget. Plans freeze skill IDs, hashes, order and budget; exports and receipts omit private file paths and full text. A changed file requires inspection and a new plan.

Recipe Memory records outcomes by task family and skill-hash combination. Only independently validated results count as successful evidence. Learned suggestions may prefer a matching combination after at least five samples and 70% observed success. This is local correlation, not proof that a skill caused success. No embeddings or cloud enrichment are used.

## Logs and documentation

Logs live at `ROUTER_HOME/logs/accension.jsonl`; the default home is the user configuration folder. Files rotate at 10 MB with five backups. Metadata links CLI, gateway, provider and task events through request/run/plan/task IDs. Central filtering drops prompts, source, skill text and provider bodies, and redacts common credentials, cookies and signed URLs. Disk failure prints a safe warning without failing the user's operation. `accs logs`, `doctor --bundle` and the Logs UI provide inspection and export.

README now starts with plain-language purpose and setup while preserving the detailed CLI reference. Skills, logging, contribution security, release notes and a writing guide are documented. Architecture, provider, routing, cache, configuration, UI, integration and advanced guides explain their terms before details. Public version branding is 1.0.0. The old migration filename became `docs/UPGRADE_NOTES.md`, and tests have descriptive names instead of version prefixes. Persisted schema/cache versions retain their original identifiers for compatibility.

## Remaining limits

CI is evidence, not a guarantee against malicious code. Plugins and registered validation commands execute trusted code. Redaction and secret scanners can miss unusual secrets. Log rotation is best-effort across simultaneous processes. Model and savings measurements are estimates, not certification or invoices. Optional embeddings and Scorecard are deferred. Provider integrations use offline mocks in normal CI; real paid-provider behavior is not certified. The release workflow is prepared but must run from reviewed `main`; this branch has not been tagged or published as a release.
