# Writing for Accension users

Start with what the user can do. Explain unfamiliar terms before using them. Use short sentences and concrete verbs. Prefer “Choose how Accension picks models” over “Configure capability-constrained execution policy.”

Write steps in the order the user performs them. Keep quick start instructions ahead of architecture details. Put advanced configuration behind an Advanced section. Keep command examples consistent with `accs --help` and say when a command can spend money or change files.

Use **Accension V1** or **1.0.0** for the public release. Configuration schema numbers, API paths and cache namespaces are internal formats, not product versions. Use GitHub's **Pull Request** or **PR** terminology.

Define the core terms simply: AXIR is a saved task plan; Model DNA is local evidence about a model's strengths; an Execution Receipt records what ran and what passed; an egress budget limits how much context may leave the computer. Keep precise details in the advanced guides.

Errors should explain what happened and what to try next. Put error IDs separately. Never include entered credentials in an error or screenshot. Do not promise guaranteed safety, guaranteed savings or causal improvements from observational scores.

Accension has no hosted account system. Do not add account-creation or sign-in controls to its UI. Provider-native authentication remains separate. Run `python scripts/check_docs.py` for links, fences, stale version wording and concrete CLI examples; human review still decides whether prose is clear.
