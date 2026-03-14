#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing Office helper venv. Run: $ROOT/scripts/bootstrap_venv.sh" >&2
  exit 2
fi
exec "$PY" "$ROOT/scripts/office_extract.py" "$@"
