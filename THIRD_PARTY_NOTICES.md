# Third-party dependencies

The Apache-2.0 license covers this repository's original code and documentation. Dependencies, optional tools and models retain their own licenses and service terms. This repository does not vendor their source, binaries, weights or personal skill content.

Direct runtime dependencies:

| Dependency | Project |
|---|---|
| FastAPI | https://github.com/fastapi/fastapi |
| Uvicorn | https://github.com/encode/uvicorn |
| HTTPX | https://github.com/encode/httpx |
| Pydantic | https://github.com/pydantic/pydantic |
| PyYAML | https://github.com/yaml/pyyaml |
| MCP Python SDK | https://github.com/modelcontextprotocol/python-sdk |

Optional Azure authentication uses `azure-identity` from https://github.com/Azure/azure-sdk-for-python. Tests use pytest and pytest-asyncio. Installed packages include their own license metadata and notices; consult those for the exact versions used. `requirements-lock.txt` records one Windows development environment and includes transitive dependencies; `pyproject.toml` declares supported dependency ranges.

RTK, Graphify, Laya, Codex, Claude, local model runtimes and provider APIs are optional integrations, not bundled components. Users install and license them separately. Mentioning an integration does not imply endorsement or affiliation.
