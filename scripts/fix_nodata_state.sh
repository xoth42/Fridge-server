#!/usr/bin/env bash
set -euo pipefail

# fix_nodata_state.sh
# Find all user-managed Grafana alert rules with noDataState != "OK" and patch them.
# Dry-run by default; use --apply to actually update.
#
# Usage:
#   ./scripts/fix_nodata_state.sh
#   ./scripts/fix_nodata_state.sh --apply
#
# Requires: jq, curl, GRAFANA_SA_TOKEN env var

GRAFANA_URL=${GRAFANA_URL:-http://localhost:3000}
GRAFANA_SA_TOKEN=${GRAFANA_SA_TOKEN:-}
APPLY=false

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "$GRAFANA_SA_TOKEN" ]]; then
  echo "Error: GRAFANA_SA_TOKEN must be set in the environment." >&2
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required (pacman -S jq)." >&2
  exit 2
fi

auth=( -H "Authorization: Bearer ${GRAFANA_SA_TOKEN}" )

all_rules=$(curl -sS "${GRAFANA_URL}/api/v1/provisioning/alert-rules" "${auth[@]}")

# Filter to rules managed by alert-api that don't already have noDataState == "OK"
matching=$(echo "$all_rules" | jq '[.[] | select(.labels.managed_by == "alert-api" and .noDataState != "OK")]')
count=$(echo "$matching" | jq 'length')

if [[ "$count" -eq 0 ]]; then
  echo "All user-managed rules already have noDataState=OK. Nothing to do."
  exit 0
fi

echo "Found $count rule(s) with noDataState != OK:"
echo "$matching" | jq -r '.[] | "  [\(.uid)] \(.title)  (currently: \(.noDataState))"'

if [[ "$APPLY" == false ]]; then
  echo ""
  echo "Dry run — no changes made. Re-run with --apply to patch."
  exit 0
fi

echo ""
errors=0
while IFS= read -r uid; do
  title=$(echo "$matching" | jq -r --arg uid "$uid" '.[] | select(.uid == $uid) | .title')
  # Fetch the full rule, patch noDataState, PUT it back.
  rule=$(curl -sS "${GRAFANA_URL}/api/v1/provisioning/alert-rules/${uid}" "${auth[@]}")
  patched=$(echo "$rule" | jq '.noDataState = "OK"')
  http_code=$(curl -sS -o /dev/null -w "%{http_code}" \
    -X PUT "${GRAFANA_URL}/api/v1/provisioning/alert-rules/${uid}" \
    -H "Content-Type: application/json" \
    -H "X-Disable-Provenance: true" \
    "${auth[@]}" \
    -d "$patched")
  if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
    echo "  Patched [$uid] $title -> noDataState=OK"
  else
    echo "  FAILED  [$uid] $title (HTTP $http_code)" >&2
    errors=$(( errors + 1 ))
  fi
done < <(echo "$matching" | jq -r '.[].uid')

if [[ "$errors" -gt 0 ]]; then
  echo "$errors rule(s) failed to update." >&2
  exit 1
fi

echo "Done. All rules patched."
