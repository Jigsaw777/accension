# Local UI

Run accs ui (or accension/router ui) to open http://127.0.0.1:8765/ui. No sign-in or sign-out flow is shown. The loopback document bootstraps a local cookie session; CSRF, same-origin, Host and fetch-metadata checks remain. External API clients still need the API token. Reload after session expiry.

Assets are bundled HTML/CSS/JavaScript with no Node build or CDN. Connected models are optional for management.

| Page | Actions |
|---|---|
| Setup | Readiness and local discovery |
| Providers | Manifest forms, credentials, health and inventory |
| Models | Paged search, enable/disable, pricing, DNA and calibration |
| Roles | Auto/preferred/pinned/disabled assignments |
| Routing | Presets, quality, budgets and control-plane locality |
| Repositories | Registered roots, privacy and trusted checks |
| Integrations | Preview/install scoped MCP registrations |
| Playground | Route preview, plan, run and execute stored plans |
| Traces | Request stages, receipts and Routing Lab |
| Costs | Spend and Savings Pulse settings/baseline |
| Cache | Inspect and clear exact cache |
| System | Doctor, theme, profiles and recovery |

The sticky Savings Pulse header remains available on every page. Click or keyboard activation opens current/latest, today and all-time breakdowns. Select current/session/today/7d/30d/all; dates use UTC. Unknown values stay unavailable and negative savings stay negative. SSE updates settle without page reload. Token display distinguishes local work from cloud/cache usage. Estimates and configured baseline provenance appear in the breakdown.

System offers system/light/dark appearance. Narrow layouts and reduced motion are supported. Mock sessions display a DEMO badge. [Screenshot gallery](docs/screenshots/README.md).

Cloud calibration requires a reviewed quote and paid opt-in. No models are downloaded automatically. Credentials are masked and never read back. Save conflicts require reload; CLI and UI share Management validation. Native CLI integration preview/apply/undo supports additional Sovereign launcher options; the UI installer remains scoped MCP.
