# Validated Workbench request examples

These synthetic JSON examples were checked against current Pydantic request models on 2026-10-12:

| Example | Current model |
|---|---|
| [prd.json](prd.json) | `workbench.schema.CreatePRD` |
| [work-item.json](work-item.json) | `workbench.schema.CreateWorkItem` |
| [command.json](command.json) | `workbench.schema.BasicCommand` |
| [agent-models.json](agent-models.json) | `workbench.schema.AgentCommand` |
| [feature-planning.json](feature-planning.json) | `workbench.schema.PlanningInput` |

Replace the illustrative workspace/project IDs with real IDs returned by your daemon. Use a fresh command ID for a new request and reuse it only for identical retries. The `tests` validation name and repository artifacts must exist in the registered target repository before execution. Schema acceptance alone does not satisfy policy, readiness, revision, authorization or provider eligibility checks.

Use these as request-shape references, not as pending work for Accension itself. Read [Workbench contracts](../WORKBENCH_CONTRACTS.md) and [the CLI reference](../CLI.md).
