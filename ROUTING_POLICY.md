# Routing policy

Routing means choosing an eligible model for a task. Accension first estimates the task type, complexity and risk using local rules and repository metadata. It then checks model capabilities, quality evidence, privacy and price. This preview makes no AI calls. More complex tasks may need a saved plan before execution.

`router route "TASK" --repo PATH` and Playground simulate routing without model calls or edits. Results show role choices, rejection reasons, fallback edges and estimated costs. These are observable decisions, not private model reasoning.

## Generic roles

Classifier, arbiter, planner, executor, fast_executor, complex_executor, reviewer, repairer, compactor, summarizer, embedding and vision accept `auto`, `preferred`, `pinned` or `disabled` policies. Locality and minimum quality are configurable. Unavailable pins fall back with an explanation; pins never bypass safety gates.

Eligibility checks capabilities, privacy, status, protocol, context, health/cooldown, resources, pricing, quality and budget. Useful cost/quality/latency candidates are compared after constraints. A tier alone neither qualifies nor excludes a model.

The semantic classifier is optional and disabled by default. If unavailable or low-confidence, deterministic classification continues. The optional arbiter obeys the same role and repository privacy gates. `control_plane.routing_location: local-only` forbids remote classifier/arbiter calls even when cloud execution is allowed. Hybrid requires an explicit policy change.

## Presets

| Preset | Behavior |
|---|---|
| Balanced | Moderate quality targets and local preference |
| Maximum Savings | Lower configurable floors and stronger local preference |
| Quality First | Higher quality floors and less local preference |
| Fully Local | Blocks built-in cloud inference, auth and metadata |
| Local Control + Cloud Compute | Local decisions; cloud work only under repository policy |
| Custom | Explicit typed settings |

Presets compile into normal policy fields and never override repository restrictions. Quality values are estimates of task success, not guarantees.

## Calibration and learning

Quick calibration uses up to three models and eleven short fixtures by default: classification, JSON/schema, coding, tests, debugging, architecture, review, tools, instructions, summarization and repository reasoning. Exact answers, JSON shape and AST properties are checked; generated code is not executed during calibration. This provides initial evidence, not comprehensive certification.

Preview with `router calibrate preview`, inspect the maximum quote, then run with `--quote-id`. Cloud runs require `--allow-paid`. Default budget: USD 0.05. Quotes include protocol/schema overhead, and calls still obey request/session/daily reservations. Unknown cloud prices block calibration.

Real outcomes update model-by-task-family success, latency, token use and cost history. Failures can reduce confidence after good calibration. `router profiles reset` clears learned profiles independently from the exact cache.

## Execution and costs

The planner is an eligible model meeting task requirements, not necessarily the most expensive model. Small work uses a shallow plan. A single model may plan and execute while deterministic registered checks provide verification.

Workers receive bounded context and acceptance criteria. Malformed structured output goes through deterministic extraction, bounded formatting repair when eligible, then normal fallback. Review/repair and uncertain charges count against the affected task. Escalation changes the failing node rather than the entire plan.

Optional context is reduced and dependency output compacted before larger-context fallback. Automatic semantic task splitting for every overflow is not implemented; a protected target that cannot fit fails clearly.

Request, session and hard daily limits use transactional reservations. Ambiguous failures retain reservations because the provider may have billed them. Displayed dollars are estimates, not invoices. Savings are not claimed without a defined counterfactual baseline.
