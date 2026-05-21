#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

START="$(date -u -d '1 day ago' '+%Y-%m-%dT%H:%M:%SZ')"
END="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

exec "${PYTHON_BIN}" "${REPO_DIR}/scripts/scrub_dodo_water_temp_artifacts.py" \
  --start "${START}" \
  --end "${END}" \
  --max-window-seconds 120 \
  "$@"