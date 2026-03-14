#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VENV="$ROOT/.venv"
python3 -m venv "$VENV"
"$VENV/bin/python" -m ensurepip --upgrade
"$VENV/bin/python" -m pip install --disable-pip-version-check --no-input -r "$ROOT/requirements.txt"
echo "Office helper environment ready: $VENV"
