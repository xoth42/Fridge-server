#!/usr/bin/env bash
# ci_bootstrap.sh — Create a Grafana SA token and wire it into .env for CI.
#
# This is a minimal extraction of the service account + token logic from
# install_alert_ui.sh, intended for use in GitHub Actions after `docker compose
# up` but before any alert-api test.  It does NOT set up notification routing,
# run e2e tests, or configure Alertmanager — those happen in later CI steps.
#
# Usage:
#   ./scripts/ci_bootstrap.sh
#
# Expects the following env vars (typically from a CI-generated .env that has
# been sourced, or directly from the GitHub Actions environment):
#   GF_ADMIN_USER         Grafana admin username (default: admin)
#   GF_ADMIN_PASSWORD     Grafana admin password (required)
#
# On success: GRAFANA_SA_TOKEN is written to .env and alert-api is restarted.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# ─── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; BLUE='\033[0;34m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
die()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# ─── Load .env ────────────────────────────────────────────────────────────────
[[ -f .env ]] || die ".env not found — run this from the repo root."
set -a; source .env; set +a

GRAFANA_URL="http://localhost:3000"
GRAFANA_ADMIN="${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}"
SA_NAME="alert-api"
SA_TOKEN_PREFIX="alert-api-token-managed"
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.ci.yml"

# ─── Helpers ──────────────────────────────────────────────────────────────────

_json_field() {
  local file="$1" filter="$2"
  jq -r "${filter} // empty" "$file" 2>/dev/null
}

_upsert_env_var() {
  local key="$1" value="$2" escaped
  escaped=$(printf '%s' "$value" | sed -e 's/[&|\\]/\\&/g')
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*$|${key}=${escaped}|" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
}

_verify_token() {
  local token="$1"
  [[ -n "$token" ]] || return 1
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "${GRAFANA_URL}/api/v1/provisioning/policies" 2>/dev/null || echo "000")
  [[ "$code" == "200" ]]
}

# ─── 1. Wait for Grafana ──────────────────────────────────────────────────────

info "Waiting for Grafana to be ready..."
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "${GRAFANA_URL}/api/health" 2>/dev/null || echo "000")
  [[ "$code" == "200" ]] && { ok "Grafana is ready."; break; }
  [[ "$i" -eq 30 ]] && die "Grafana did not become healthy after 60s (last HTTP: $code)"
  sleep 2
done

# ─── 2. Create or locate the service account ──────────────────────────────────

info "Looking up service account '${SA_NAME}'..."
SA_BODY=$(mktemp)
SA_CODE=$(curl -sS -o "$SA_BODY" -w '%{http_code}' \
  -u "$GRAFANA_ADMIN" \
  "${GRAFANA_URL}/api/serviceaccounts/search?query=${SA_NAME}" 2>/dev/null || echo "000")
[[ "$SA_CODE" == "200" ]] || die "SA search failed (HTTP $SA_CODE): $(cat "$SA_BODY")"

SA_ID=$(_json_field "$SA_BODY" '(.serviceAccounts // [])[0].id')
SA_ROLE=$(_json_field "$SA_BODY" '(.serviceAccounts // [])[0].role')
rm -f "$SA_BODY"

if [[ -z "$SA_ID" || "$SA_ID" == "null" ]]; then
  info "Creating service account '${SA_NAME}'..."
  CREATE_BODY=$(mktemp)
  CREATE_CODE=$(curl -sS -o "$CREATE_BODY" -w '%{http_code}' \
    -X POST -u "$GRAFANA_ADMIN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${SA_NAME}\",\"role\":\"Admin\",\"isDisabled\":false}" \
    "${GRAFANA_URL}/api/serviceaccounts" 2>/dev/null || echo "000")
  [[ "$CREATE_CODE" == "200" ]] || die "SA creation failed (HTTP $CREATE_CODE): $(cat "$CREATE_BODY")"
  SA_ID=$(_json_field "$CREATE_BODY" '.id')
  rm -f "$CREATE_BODY"
  ok "Service account created (id=${SA_ID})."
else
  ok "Service account exists (id=${SA_ID}, role=${SA_ROLE})."
fi

# Ensure Admin role.
if [[ "$SA_ROLE" != "Admin" ]]; then
  info "Upgrading service account role to Admin..."
  PATCH_BODY=$(mktemp)
  PATCH_CODE=$(curl -sS -o "$PATCH_BODY" -w '%{http_code}' \
    -X PATCH -u "$GRAFANA_ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"role":"Admin"}' \
    "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}" 2>/dev/null || echo "000")
  [[ "$PATCH_CODE" == "200" ]] || die "SA role upgrade failed (HTTP $PATCH_CODE): $(cat "$PATCH_BODY")"
  rm -f "$PATCH_BODY"
  ok "Service account role set to Admin."
fi

# ─── 3. Mint a fresh token ────────────────────────────────────────────────────
# Always create a new token in CI — no need to check/reuse an existing one.
# Delete any stale installer-managed tokens first (idempotent).

info "Cleaning up old managed tokens..."
LIST_BODY=$(mktemp)
LIST_CODE=$(curl -sS -o "$LIST_BODY" -w '%{http_code}' \
  -u "$GRAFANA_ADMIN" \
  "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}/tokens" 2>/dev/null || echo "000")
[[ "$LIST_CODE" == "200" ]] || die "Token list failed (HTTP $LIST_CODE): $(cat "$LIST_BODY")"

mapfile -t OLD_IDS < <(
  jq -r --arg prefix "$SA_TOKEN_PREFIX" \
    '.[] | select(.name | startswith($prefix)) | .id' \
    "$LIST_BODY" 2>/dev/null
)
rm -f "$LIST_BODY"

for token_id in "${OLD_IDS[@]}"; do
  curl -sS -X DELETE -u "$GRAFANA_ADMIN" \
    "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}/tokens/${token_id}" >/dev/null 2>&1 || true
done

info "Creating new service account token..."
TOKEN_NAME="${SA_TOKEN_PREFIX}-$(date +%Y%m%d%H%M%S)"
TOKEN_BODY=$(mktemp)
TOKEN_CODE=$(curl -sS -o "$TOKEN_BODY" -w '%{http_code}' \
  -X POST -u "$GRAFANA_ADMIN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"${TOKEN_NAME}\"}" \
  "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}/tokens" 2>/dev/null || echo "000")
[[ "$TOKEN_CODE" == "200" ]] || die "Token creation failed (HTTP $TOKEN_CODE): $(cat "$TOKEN_BODY")"

SA_TOKEN=$(_json_field "$TOKEN_BODY" '.key')
rm -f "$TOKEN_BODY"
[[ -n "$SA_TOKEN" && "$SA_TOKEN" != "null" ]] || die "Token created but key field missing."

# ─── 4. Write token to .env and restart alert-api ────────────────────────────

_upsert_env_var "GRAFANA_SA_TOKEN" "$SA_TOKEN"
export GRAFANA_SA_TOKEN="$SA_TOKEN"
ok "GRAFANA_SA_TOKEN written to .env."

info "Recreating alert-api with new token..."
# docker compose restart does NOT reload environment variables — it keeps the
# original container env. Use up --force-recreate to get a fresh container
# that reads GRAFANA_SA_TOKEN from the updated .env.
# shellcheck disable=SC2086
docker compose $COMPOSE_FILES up -d --force-recreate alert-api >/dev/null 2>&1

# ─── 5. Wait for alert-api to report grafana reachable ───────────────────────

info "Waiting for alert-api to be healthy..."
for i in $(seq 1 15); do
  body=$(curl -s "http://localhost:8000/api/health" 2>/dev/null || echo "{}")
  if echo "$body" | grep -q '"grafana":"reachable"'; then
    ok "Alert-api is healthy and connected to Grafana."
    exit 0
  fi
  [[ "$i" -eq 15 ]] && die "Alert-api did not become healthy after 30s. Last response: $body"
  sleep 2
done
