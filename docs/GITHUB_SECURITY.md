# How changes reach main

A Pull Request (PR) is GitHub's name for a proposed code change. Every contributor change must pass automated checks and owner review before it can enter `main`. Passing CI does not merge a PR automatically.

Two active rulesets separate review from checks:

| Rule | Policy |
| --- | --- |
| Protect main | A PR, one approval, Code Owner review, stale-approval dismissal and resolved review conversations |
| Owner | `@Jigsaw777` owns every path through `.github/CODEOWNERS` |
| Owner-authored PR | Repository admins have a **PR-only** bypass for the self-review deadlock; it does not allow direct pushes |
| Main checks and history | Requires the GitHub Actions `Merge Gate`, an up-to-date branch, linear history, and blocks force pushes and deletion; no bypass actors |
| Merge strategy | Squash only; ordinary merge commits and rebase merging are disabled |

The owner must still open a PR and pass the non-bypassable checks. Normal contributors should never receive admin access as a way around review.

Signed commits are strongly recommended, but are not mandatory for contributors' intermediate commits. GitHub's signed-commit rule can block a squash merge with unsigned commits. We prefer GitHub-generated verified squash merges without adding that onboarding barrier. See [GitHub's rules documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

## Automated checks

`Merge Gate` depends on code/docs checks, all nine Ubuntu/Windows/macOS × Python 3.11/3.12/3.13 test jobs, Python and JavaScript CodeQL, dependency audit, PR dependency review, secret audit, workflow security and clean wheel installation. Failed, cancelled or unexpectedly skipped checks block the gate. Only dependency review is skipped outside PR events.

CI runs on pull requests, pushes to main, scheduled audits and manual runs. New PR commits cancel outdated runs for that PR. Feature-branch pushes do not start a duplicate matrix alongside the PR run.

After CodeQL finishes, a local SARIF-report check blocks medium-or-higher security findings and other error-level findings. A missing report fails the job. This prevents a successful scan upload from being mistaken for a clean result. GitHub also supports [code-scanning merge protection](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/manage-your-configuration/set-merge-protection); this repository enforces alert thresholds inside its required Merge Gate.

Ruff checks syntax, unused imports, common mistakes and formatting. Bandit blocks medium/high-severity findings with high confidence. `pip-audit` checks installed dependencies, including optional provider integrations. There are no vulnerability-ID exceptions. The editable project itself is skipped because it is local source, not a published dependency. Dependabot proposes weekly dependency and Actions updates; it cannot auto-merge them.

Tests use mock providers and block unexpected external socket connections in the test process. They require no paid provider keys. Subprocess validation remains trusted repository code; run tests for untrusted contributions in an isolated environment. CodeQL receives only the permissions needed for code scanning. No PR workflow uses `pull_request_target`, stored cloud credentials, persistent checkout credentials or a repository write token. Actions are pinned to full SHAs; zizmor checks the workflows offline.

`scripts/security_policy.py` flags sensitive calls such as process launches, dynamic imports, unsafe YAML, deserialization, shell/redirect settings and listeners. Its allowlist binds approvals to exact AST fingerprints and counts. Each entry explains the intended boundary. New or changed calls require explicit review. This is a drift detector, not a sandbox or proof that code is harmless.

## Secret protection

Gitleaks uses a pinned release and SHA-256 checksum. PR checks scan the proposed history range. Branch/scheduled checks scan all reachable history. `scan_public.py` also rejects private tracked paths, local databases, credential files and raw logs. GitHub secret scanning and push protection provide another layer.

For a historical finding, confirm it without publishing its value. Revoke or rotate real credentials first. Only then back up the repository, record affected refs, remove the material using `git-filter-repo`'s sensitive-data workflow, rescan, and coordinate the authorized history update. Never rewrite history for an unconfirmed heuristic finding.

## Maintainer setup and verification

```sh
python scripts/configure_github_security.py --dry-run
python scripts/configure_github_security.py --apply
python scripts/configure_github_security.py --verify
```

The script uses existing `gh` authentication. It never stores tokens. Apply updates named rulesets idempotently, enables Discussions and available security settings, and reads the live state back. Verify is an optional online maintainer check; normal Accension operation does not depend on GitHub.

Bootstrap caveat: the new CODEOWNERS file must enter `main` through the first reviewed PR before GitHub can resolve its ownership rules. A settings flag alone is not proof that CODEOWNERS is already on the default branch.

## Releases

The manual **Release artifacts** workflow runs only from protected `main`, repeats mandatory checks, builds and smoke-tests the wheel, and produces a dependency SBOM, checksums and a provenance attestation. It uploads a release candidate for maintainer inspection. It neither tags nor publishes automatically. Create `v1.0.0` and the public release only after owner review, passing checks and explicit release approval.

CI reduces risk; it cannot guarantee that contributed code is free of malicious behavior. Owner review remains essential. OpenSSF Scorecard is optional and deferred; the mandatory checks above provide the initial security gate.
