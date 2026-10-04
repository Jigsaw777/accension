# Accension 1.0.0

## What Accension does

Accension helps your AI models work together. It chooses models for a task, controls spending, checks repository changes and records the result. It runs on your computer and is open source. Cloud providers charge only when you choose to use them; local models have no API-token charges.

## What's in V1

- A terminal CLI and local browser app for connecting providers and running tasks.
- Companion mode for tasks delegated by your host assistant, and Sovereign mode for routing supported client traffic.
- Saved task plans, model performance evidence, checked edits, recovery and execution receipts.
- Skills, reusable presets, offline suggestions and local recipe evidence.
- Savings estimates and redacted logs linked to task traces.

## Getting started

Install from the reviewed source checkout, run `accs init`, connect a model, and open `accs ui` or use the CLI. Follow the [quick start](../README.md#install-and-start-in-the-terminal). A PyPI publication is not assumed.

## Security

Provider keys stay in a local credential store or environment references. Repository changes use registered roots, hash checks and trusted validation commands. Skills cannot grant tools or relax privacy or budgets. Contributions require automated checks and maintainer review. Read the [security policy](../SECURITY.md).

## Known limitations

Validation and third-party provider plugins execute trusted code; they are not sandboxes. Secret scanning and redaction are imperfect. Savings are estimates, not invoices. Model/skill outcomes are observations, not causal proof. Skill search is lexical; embedding search is not included. Provider behavior varies and only mocked calls run in normal CI. Review logs before sharing them.

## How to contribute

Open a PR with a clear explanation and tests. The owner reviews contributor changes after CI passes. See [Contributing](../CONTRIBUTING.md). Report vulnerabilities privately through GitHub, not public issues.
