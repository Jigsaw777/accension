#!/bin/sh
set -eu
ROUTER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m venv "$ROUTER_ROOT/.venv"
"$ROUTER_ROOT/.venv/bin/python" -m pip install -e "$ROUTER_ROOT[test]"
"$ROUTER_ROOT/.venv/bin/python" -m local_ai_router.cli --home "$ROUTER_ROOT" config validate

