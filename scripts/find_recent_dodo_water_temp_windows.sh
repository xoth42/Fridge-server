#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "${PYTHON_BIN}" "${REPO_DIR}/scripts/scrub_dodo_water_temp_artifacts.py" \
  --past-hours 24 \
  --max-window-seconds 120 \
  "$@"