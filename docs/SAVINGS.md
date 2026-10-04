# Savings Pulse methodology

Savings Pulse compares recorded API usage with an estimated alternative model cost. It appears in the CLI, app, MCP tools and receipts. Savings and avoided cloud tokens are estimates, not provider invoices. Recorded API cost uses reported usage and the prices saved when the task ran.

## Baseline

Choose an available registered cloud model under Costs → Savings settings. None is configured by default. DIRECT_MODEL and USER_SELECTED_MODEL use the configured model. HOST_MODEL uses an eligible registered host model when supplied; virtual aliases alone do not identify one. QUALITY_BASELINE uses quality-first eligible cloud evidence, not the highest price. DISABLED disables the comparison.

```yaml
savings:
  enabled: true
  baseline_method: DIRECT_MODEL
  baseline_model: YOUR_INSTANCE:YOUR_MODEL
  header_period: today
  show_tokens: true
  show_percentage: true
  currency: USD
```

Header periods: current/latest, session, today, 7d, 30d and all. Days use UTC. No baseline means unavailable savings. Disabling tracking preserves history and lets already-open groups finish.

## Comparable workload

Each logical planner, worker or reviewer stage has one baseline equivalent from its first observed attempt with usage. Input/output counts are normalized across models, not exact cross-vendor tokenization. A worker adds measured omitted context once. Retries, failures and classifier/arbiter overhead stay in actual cost without multiplying baseline work. Plan reuse uses an observed original planner call when available.

```text
actual API cost = sum of observed calls at frozen per-call prices
baseline estimate = equivalent logical stages at frozen baseline prices
estimated savings = baseline estimate − actual API cost
estimated savings % = savings / positive baseline estimate × 100
```

Decimal strings and Decimal accumulation preserve arithmetic. Unknown usage or unknown/non-USD pricing yields unknown costs. Negative savings stay negative. Known partial cost is never shown as a complete total.

Local input/output, cloud input/output, cached cloud tokens, cache writes, omitted context, plan reuse and frontier calls are distinct. Cached tokens remain cloud tokens and use configured cache rates, including zero; they are not local work. Paid cloud tokens avoided subtract observed cloud tokens from baseline equivalents and can be negative. Legacy tier 4 identifies the configured frontier category, not a universal vendor fact.

Local API cost is zero. Hardware/power/hosting cost estimates are not invented. Context counts cover the measured capsule subset, not the whole repository. Attribution factors explain contributors and are not additive dollar subtotals.

## Persistence and live updates

SQLite stores usage facts, frozen prices and snapshots. Finalization atomically updates the receipt and all/day/session materialized totals once. Header reads indexed totals plus active snapshots, without scanning historical usage. Scale coverage uses 100,000 runs and two million usage rows.

Coalesced SSE emits local bursts at most every 250 ms and checks cross-process revisions once per second. EventSource reconnects automatically. There is no request per streamed token. Click or keyboard activation opens details; Escape dismisses; reduced-motion CSS is supported.

Plans/interrupted runs are excluded from finalized totals; active snapshots overlay the header. Process-owner recovery removes abandoned activity. Optional analytics failures cannot fail inference. Missing records reconcile against the budget ledger. Reservation, settlement and finalization failures create durable gap markers where needed so totals stay unknown after restart; successful receipt finalization clears the relevant markers. A full storage outage can prevent even a marker from persisting.

## Inspect and compare

```sh
accs savings --today
accs savings --all --json
accs savings --session SESSION_ID
accs savings --run RUN_ID
accs savings --run RUN_ID --reprice
accs lab compare RUN_ID
```

Historical economics does not change with prices/baseline settings. Repricing returns separate current-price analysis. Lab applies current eligible preset choices to normalized observed worker tokens (plan estimates if absent), holds recorded nonworker/retry overhead fixed, and shows the recorded direct baseline. Alternative cache discounts, output lengths, retries and quality can differ. There are zero shadow calls and no measured alternative-savings claim.

The costs command is the conservative monetary reservation ledger. It can differ from analytics when failed calls retain reservations. Inspect the receipt and configuration rather than changing history to force agreement.
