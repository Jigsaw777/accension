# Execution Receipts

```sh
accs receipt RUN_ID
accs receipt RUN_ID --json --output receipt.json
accs receipt RUN_ID --markdown --output receipt.md
accs savings --run RUN_ID
```

Repository receipts record plan/version/hash, observed model/provider/role calls, usage, frozen economics, cloud egress, changed-file before/written/after hashes, patch-manifest hash, registered checks, git state, repairs/fallbacks, status and timestamps. Patch hash means the ordered path/before/written manifest, not a textual diff.

Gateway/direct-inference receipts contain only available call evidence, without repository validation/patch claims. JSON and Markdown support both forms. MCP receipt and native /accs/v1/receipts/{run_id} read the same stored record as CLI/UI.

Finalized economics is immutable and contributes to aggregate totals once. Recovery may update repository status/file evidence while preserving original economics. Repricing returns separate analysis. Paused/interrupted runs do not count as finalized savings; active header estimates are separate.

No full prompts or hidden reasoning are stored in receipts. Filenames and model IDs can still be sensitive. Evidence is local, unsigned and not externally attested or reconciled with invoices. Missing usage/pricing is incomplete. Disk failure can prevent saving; execution reports the failure and releases its active-run guard.
