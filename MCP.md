# MCP, task API and gateway

accs mcp starts the stdio server. The compatibility server name stays local-ai-router. A running hub also exposes authenticated Streamable HTTP at /mcp. Both use the same engine, contracts, usage and receipts.

## Companion tools

| Tool | Purpose |
|---|---|
| orchestrate_feature / orchestrate_bugfix | Compile and execute an authorized registered repository task |
| route_task / explain_route | No-inference eligibility explanation |
| estimate_cost | Routing range and baseline economics, no shadow calls |
| plan_task / execute_plan | Separate reviewable planning and execution |
| receipt | Stored models, hashes, validation, egress and economics |
| router_status | Hub or run readiness |
| router_info | Paged models, providers, roles, recovery, costs, savings, cache, trace and budget |

Example orchestrate_feature arguments:

```json
{
  "task": "Add a greeting function with named and blank input tests",
  "repo_path": "/path/to/project",
  "constraints": ["Preserve public interfaces"],
  "budget": 0.50,
  "execution_mode": "plan-only",
  "privacy": "LOCAL_ONLY",
  "max_cloud_context": 0
}
```

Inspect the stored plan before execute_plan, or use safe-auto for planning and execution within registered boundaries. The host decides when to delegate and retains its conversation model. MCP grants file-changing capabilities, not a sandbox or authorization beyond the user's request.

## Native task API

All routes use the separate local API token (Authorization: Bearer). POST bodies are bounded JSON; task bodies follow the same Request schema as CLI/MCP.

| Method and path | Result |
|---|---|
| POST /accs/v1/route | No-inference route |
| POST /accs/v1/plans | Plan ID, request ID and AXIR |
| GET /accs/v1/plans/{id} | Portable plan |
| POST /accs/v1/plans/import | Fresh bound plan from repo_path and axir |
| POST /accs/v1/plans/{id}/execute | Execute with repo_path |
| POST /accs/v1/tasks | Compile/run a task |
| GET /accs/v1/runs/{id} | Status and receipt availability |
| GET /accs/v1/receipts/{id} | Stored evidence |
| GET /accs/v1/savings?period=today | Shared savings summary |

Task calls are awaited requests, not a distributed job scheduler. CLI execution and UI updates share durable state.

## Native inference gateway

OpenAI Chat/Responses and Anthropic Messages stay on a fast compatible forwarding path, preserving supported tools/SSE. Gateway requests do not automatically compile AXIR. Eligible downstream protocols are required; there is no universal Bedrock/Gemini translation.

MCP delegation and gateway transport are distinct. Downstream HTTP carries X-Local-Router-Hop; reentry and self-pointing endpoints are rejected. External MCP workers must not recursively invoke the orchestrator. Do not delegate Accension's own implementation to itself.
