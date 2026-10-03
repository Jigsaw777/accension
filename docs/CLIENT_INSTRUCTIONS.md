# Router usage instructions

For nontrivial feature implementation, architecture, multi-file bug fixes or large refactors in a registered repository, prefer local-ai-router's orchestrate_feature or orchestrate_bugfix tool. Pass the user's goal and constraints concisely. The router owns decomposition, model choice, file edits, validation and targeted escalation. Use the host model for interaction and final review.

First check router_info status if availability is unknown. Unregistered repositories, missing validation commands or unavailable eligible providers are blockers; report them without recursive delegation. Do not invoke the router to develop local-ai-router itself. Trivial tasks can be handled directly. Never treat MCP as replacement of the host's primary model transport. Do not bypass existing user restrictions or approvals.

