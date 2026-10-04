# AXIR v1

AXIR is the portable format for a saved Accension task plan. It records the work to do, its limits and the checks required. You can inspect the plan, choose suitable models on another machine and execute it against a registered repository. AXIR stands for Accension Execution Intermediate Representation.

```sh
accs plan "Refactor authentication" --repo . --export auth.axir.json
accs inspect auth.axir.json
accs reroute auth.axir.json
accs plan diff previous.axir.json auth.axir.json
accs execute auth.axir.json --repo .
```

The envelope carries goal, assumptions, constraints, repository-content fingerprint, privacy/egress/quality contracts, dependency graph, capability requirements, artifacts, validation names, fallback/completion policy and provenance. Export omits credentials, absolute repository paths and executable validation commands. Goals and filenames can still be sensitive; inspect before sharing.

Import creates fresh local plan/request IDs, validates cycles and paths, merges global capability requirements into nodes, checks the registered repository fingerprint and permits only operator-registered validation names. Nodes cannot weaken repository/global privacy. Content changes invalidate the fingerprint even when size and modification time match.

Reroute previews binding using current evidence without source changes or inference. Execute binds to the supplied registered root and uses the guarded executor. AXIR grants no arbitrary shell execution or authorization. Unknown checks, stale source and unsupported schemas fail before execution.

The repository graph selects context; the AXIR graph defines dependencies and write scopes. Nonconflicting nodes may run concurrently. Simple tasks can use one worker node. Distributed execution and remote attestation are not implemented.
