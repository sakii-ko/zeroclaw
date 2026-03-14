#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing video-fetch runtime. Run: $HOME/utils/zeroclaw/bin/video-fetch-bootstrap" >&2
  exit 2
fi
exec "$PY" "$ROOT/scripts/video_fetch.py" "$@"
