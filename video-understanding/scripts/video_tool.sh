#!/usr/bin/env bash
set -euo pipefail
exec python3 "$HOME/utils/zeroclaw/video-understanding/scripts/video_tool.py" "$@"
