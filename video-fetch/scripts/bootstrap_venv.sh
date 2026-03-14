#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 -m venv "$ROOT/.venv"
source "$ROOT/.venv/bin/activate"
pip install -U pip
pip install -U --pre -r "$ROOT/requirements.txt"
