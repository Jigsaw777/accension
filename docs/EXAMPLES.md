# Actual CLI examples

Captured from the deterministic mock workspace. These are demo observations, not production savings or model benchmarks. Commands use the isolated demo configuration home; that private path is omitted below.

## accs --help

```text
usage: accs [-h] [--version] [--home HOME] [--mock] [--json] [--no-color]
            [--debug]
            COMMAND ...

Accension — local AI execution compiler and control plane

positional arguments:
  COMMAND
    serve        Run the local hub in this terminal
    ui           Open the local UI
    start        Start a user-level background hub
    stop         Manage the local hub
    restart      Manage the local hub
    status       Manage the local hub
    doctor
    costs
    mcp
    enroll
    discover
    azure-login
    version
    init         Terminal setup; no browser required
    route        Simulate a route without inference or edits
    run          Compile, execute, verify and record a receipt
    plan         Compile a task into a portable execution graph
    execute      Execute a stored plan or portable AXIR file
    inspect
    reroute
    resume
    rollback
    trace
    receipt      Read execution evidence
    provider     Connect any number of provider instances
    model        Browse, probe and inspect Model DNA
    models       Legacy model-list alias
    role         Explain or configure capability-based role assignments
    calibrate    Preview or run bounded objective fixtures
    plugin       Inspect built-in and explicitly trusted plugins
    integrate    Preview, apply or undo a client integration
    launch       Launch a client through the hub, or use --direct
    mode
    lab          Compare historical routing policies without shadow calls
    savings      Read local estimated savings and usage
    cache
    config
    repo
    completion
    eval
    profiles
    integration
    recovery
    demo

options:
  -h, --help     show this help message and exit
  --version      show program's version number and exit
  --home HOME    Configuration home
  --mock         Deterministic fixture models only
  --json         Machine-readable JSON output
  --no-color     Plain output (also the default)
  --debug        Show diagnostic traceback on failure
```

## accs route "Update documentation" --explain

```text
Route: mock-worker
Task: simple · Planning: NONE
Privacy: CLOUD_ALLOWED
Expected model cost: USD 0.000000 (estimate)
Fallbacks: mock-worker → demo-baseline
Reasons: DETERMINISTIC_PRIOR, LOCAL_POLICY
Inference calls: 0 · Repository edits: 0
Baseline model: demo-baseline
Illustrative routed cost: USD 0.000000 – USD 0.000000
Estimated baseline range: USD 0.030000 – USD 0.120000
Estimated savings range: USD 0.030000 – USD 0.120000
Basis: normalized single-stage workload; compilation/retries can add cost. Use --json for full methodology.
```

## accs model dna mock-worker

```text
Model DNA: mock-worker
Capability             Estimate   Samples   Evidence
classification         unknown    0         unknown
planning               unknown    0         unknown
architecture           unknown    0         unknown
coding                 0.525      1         limited
debugging              unknown    0         unknown
testing                unknown    0         unknown
review                 unknown    0         unknown
repair                 unknown    0         unknown
tool_use               unknown    0         unknown
structured_output      unknown    0         unknown
instruction_following  unknown    0         unknown
long_context           unknown    0         unknown
documentation          unknown    0         unknown
reasoning              unknown    0         unknown
reliability            0.593      4         limited
cost_efficiency        0.525      1         limited
```

## accs receipt RUN_ID

```text
Execution receipt: 12a697e58abb4e0ab40fde1a98231bca
Status: complete
Models: mock-worker
Estimated cost: USD 0.000000
Cloud context: 0 tokens (upper bound)
Validation: 2/2 checks passed
Files: USAGE.md, greeting.py, test_greeting.py
Patch manifest SHA-256: 96eb5ed249776d224175718059072033658b4ec7f3adb022a1a320225a264731
RUN ECONOMICS
API cost from usage: USD 0.000000
Estimated direct baseline: USD 0.018970
Estimated savings: USD 0.018970
Estimated reduction: 100.0%
Paid cloud tokens avoided (est.): 1432
Local tokens processed: 1432
Cloud tokens processed (including cache): 0
Provider cached tokens: 0
Coverage: recorded usage; counterfactual remains estimated
```
