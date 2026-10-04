# Cloud egress budgets

Egress means information sent out to a cloud provider. A cloud context budget limits how much task input can leave your computer across all calls in a request. This is separate from a money budget. Use `accs run TASK --repo . --max-cloud-context 12000` to set a request limit. Global and repository settings can set stricter defaults.

An SQLite transaction reserves an input-token upper bound and unique file set before transmission, preventing parallel races. Confirmed unsent calls can release reservations; ambiguous delivery retains them. This conservative serialized-input bound includes system/schema overhead and is not exact provider tokenization. Output is controlled by monetary/output caps.

Contracts follow planner, worker, reviewer, repair and control calls. Local-only dependencies/shared artifacts cannot flow silently into cloud nodes. Repository/global policy remains the ceiling. Planning uses a symbol/interface manifest; workers preserve complete edit targets and required constraints while optional context can shrink.

Receipts expose cloud calls, context bounds, files and uncertain deliveries. Gateway clients can set metadata.accension_repository to an exactly registered root; absent metadata means global/role policy only.

Fully Local blocks built-in cloud inference, discovery, auth refresh and embeddings. Trusted plugins, commands and external local servers have independent behavior; process-level prohibitions require OS controls.
