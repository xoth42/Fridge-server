#!/usr/bin/env bash
set -euo pipefail

# cleanup_provisioned_alerts.sh
# List or delete Grafana provisioning alert rules (requires GRAFANA_SA_TOKEN).
# Usage:
#   ./scripts/cleanup_provisioned_alerts.sh --list
#   ./scripts/cleanup_provisioned_alerts.sh --delete <UID> [UID...]
#   ./scripts/cleanup_provisioned_alerts.sh --delete-matching <jq-filter>
# Examples:
#   ./scripts/cleanup_provisioned_alerts.sh --list
#   ./scripts/cleanup_provisioned_alerts.sh --delete staleness-dodo
#   ./scripts/cleanup_provisioned_alerts.sh --delete-matching '.[] | select(.title|test("staleness")) | .uid'

GRAFANA_URL=${GRAFANA_URL:-http://localhost:3000}
GRAFANA_SA_TOKEN=${GRAFANA_SA_TOKEN:-}

if [[ -z "$GRAFANA_SA_TOKEN" ]]; then
  echo "Error: GRAFANA_SA_TOKEN must be set in the environment." >&2
  echo "Export it or run: export GRAFANA_SA_TOKEN=..." >&2
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required. Install it (apt install jq or pacman -S jq)." >&2
  exit 2
fi

cmd="$1"; shift || cmd="--list"

auth_header=( -H "Authorization: Bearer ${GRAFANA_SA_TOKEN}" )

case "$cmd" in
  --list)
    curl -sS "${GRAFANA_URL}/api/v1/provisioning/alert-rules" "${auth_header[@]}" | jq '.[] | {uid: .uid, title: .title, folder: .folder, labels: .labels}'
    ;;

  --delete)
    if [[ $# -lt 1 ]]; then
      echo "Usage: $0 --delete <UID> [UID...]" >&2
      exit 2
    fi
    for uid in "$@"; do
      echo "Deleting provisioning alert rule uid=$uid ..."
      curl -sS -X DELETE "${GRAFANA_URL}/api/v1/provisioning/alert-rules/${uid}" -H "X-Disable-Provenance: true" "${auth_header[@]}" || {
        echo "Failed to delete uid=${uid}" >&2
      }
    done
    ;;

  --delete-matching)
    if [[ $# -ne 1 ]]; then
      echo "Usage: $0 --delete-matching '<jq-filter>'" >&2
      echo "Example filter to delete titles containing 'staleness': .[] | select(.title|test(\"staleness\")) | .uid" >&2
      exit 2
    fi
    filter="$1"
    uids=$(curl -sS "${GRAFANA_URL}/api/v1/provisioning/alert-rules" "${auth_header[@]}" | jq -r "$filter")
    if [[ -z "$uids" ]]; then
      echo "No matching rules found.";
      exit 0
    fi
    echo "$uids" | while read -r uid; do
      if [[ -n "$uid" && "$uid" != "null" ]]; then
        echo "Deleting uid=$uid ..."
        curl -sS -X DELETE "${GRAFANA_URL}/api/v1/provisioning/alert-rules/${uid}" -H "X-Disable-Provenance: true" "${auth_header[@]}" || echo "Failed uid=$uid" >&2
      fi
    done
    ;;

  *)
    echo "Unknown command: $cmd" >&2
    echo "Usage: $0 --list | --delete <UID> [UID...] | --delete-matching '<jq-filter>'" >&2
    exit 2
    ;;
esac
