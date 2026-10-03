#!/bin/sh
set -eu
ROUTER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$ROUTER_ROOT/.venv/bin/python" -m local_ai_router.cli --home "$ROUTER_ROOT" serve
