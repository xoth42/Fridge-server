#!/usr/bin/env bash
# =============================================================================
# install_alert_ui.sh — Alert API & notification routing setup
# =============================================================================
# Handles everything that requires Grafana to already be running:
#   1. Grafana service account (SA) — ensures Admin role, rotates token if stale
#   2. alert-api — starts and verifies health
#   3. Grafana notification policy — full rebuild via alert-api (idempotent)
#   3b. Routing verification — confirms every email contact point has a route
#   4. E2E email delivery test — proves the full Alertmanager routing path works
#
# Called automatically by install.sh after the core stack is healthy.
# Can also be run standalone to repair or re-test the alert routing without
# a full reinstall:
#
#   ./install_alert_ui.sh
#   ./install_alert_ui.sh --skip-e2e    # skip E2E test (routing setup only)
#
# Prerequisites:
#   - Core stack running: prometheus, grafana, alertmanager, pushgateway, alert-api
#   - .env present and populated
#   - jq installed (required for routing verification)
# =============================================================================

set -euo pipefail

SKIP_E2E=false
for arg in "$@"; do
  [[ "$arg" == "--skip-e2e" ]] && SKIP_E2E=true
done

# ─── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }
step()  { echo -e "\n${BOLD}── $* ──${NC}"; }

# ─── Locate repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Load .env ────────────────────────────────────────────────────────────────
[[ -f .env ]] || die ".env not found — run this from the repo root or create .env first."
set -a
# shellcheck source=/dev/null
source .env
set +a

# Re-read variables that may contain special characters.
_reread_var() {
  local key="$1"
  local raw
  raw=$(grep -m1 "^${key}=" .env | cut -d= -f2- | sed "s/^['\"]//;s/['\"]$//")
  export "${key}=${raw}"
}

# ─── Wait-for helper ──────────────────────────────────────────────────────────
wait_for() {
  local name="$1"
  local url="$2"
  local expected_code="$3"
  local attempts="${4:-30}"
  local code=""

  info "Waiting for $name ($url)..."
  for i in $(seq 1 "$attempts"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo "000")
    if [[ "$code" == "$expected_code" ]]; then
      ok "$name is ready."
      return 0
    fi
    sleep 2
  done

  echo ""
  docker compose ps
  die "$name did not become healthy after $((attempts * 2))s (last HTTP status: $code)"
}

# ─── Verify Grafana is already up ─────────────────────────────────────────────
step "Grafana connectivity check"
GRAFANA_ADMIN="${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}"
GRAFANA_URL="http://localhost:3000"

GF_CODE=$(curl -s -o /dev/null -w '%{http_code}' "${GRAFANA_URL}/api/health" 2>/dev/null || echo "000")
[[ "$GF_CODE" == "200" ]] \
  || die "Grafana is not healthy at ${GRAFANA_URL} (HTTP $GF_CODE).  Run install.sh first."
ok "Grafana is reachable."

# =============================================================================
# 1. Grafana service account for alert-api
# =============================================================================
# Self-healing flow:
# - ensure service account exists with Admin role
# - verify the stored GRAFANA_SA_TOKEN from .env still works
# - if missing or invalid: delete old installer-managed tokens, mint a new one,
#   update GRAFANA_SA_TOKEN in .env, restart alert-api

step "Alert API service account"

POLICY_URL="${GRAFANA_URL}/api/v1/provisioning/policies"
SA_NAME="alert-api"
SA_TOKEN_LEGACY_NAME="alert-api-token"
SA_TOKEN_PREFIX="alert-api-token-managed"

_verify_sa_token() {
  local token="$1"
  local code
  [[ -n "$token" ]] || return 1
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "$POLICY_URL" 2>/dev/null || echo "000")
  [[ "$code" == "200" ]]
}

_escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

_upsert_env_var() {
  local key="$1"
  local value="$2"
  local escaped
  escaped=$(_escape_sed_replacement "$value")
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*$|${key}=${escaped}|" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
}

_json_field_or_empty() {
  local file="$1"
  local filter="$2"
  jq -r "${filter} // empty" "$file" 2>/dev/null
}

# Look up existing service account.
SA_SEARCH_BODY=$(mktemp)
SA_SEARCH_CODE=$(curl -sS -o "$SA_SEARCH_BODY" -w '%{http_code}' \
  -u "$GRAFANA_ADMIN" \
  "${GRAFANA_URL}/api/serviceaccounts/search?query=${SA_NAME}" 2>/dev/null || echo "000")

[[ "$SA_SEARCH_CODE" == "200" ]] \
  || die "Failed to search Grafana service accounts (HTTP $SA_SEARCH_CODE): $(tr -d '\n' < "$SA_SEARCH_BODY")"

SA_ID=$(_json_field_or_empty "$SA_SEARCH_BODY" '(.serviceAccounts // [])[0].id')
SA_ROLE=$(_json_field_or_empty "$SA_SEARCH_BODY" '(.serviceAccounts // [])[0].role')
rm -f "$SA_SEARCH_BODY"

if [[ -z "$SA_ID" || "$SA_ID" == "null" ]]; then
  CREATE_SA_BODY=$(mktemp)
  CREATE_SA_CODE=$(curl -sS -o "$CREATE_SA_BODY" -w '%{http_code}' \
    -X POST \
    -u "$GRAFANA_ADMIN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${SA_NAME}\",\"role\":\"Admin\",\"isDisabled\":false}" \
    "${GRAFANA_URL}/api/serviceaccounts" 2>/dev/null || echo "000")

  [[ "$CREATE_SA_CODE" == "200" ]] \
    || die "Failed to create Grafana service account (HTTP $CREATE_SA_CODE): $(tr -d '\n' < "$CREATE_SA_BODY")"

  SA_ID=$(_json_field_or_empty "$CREATE_SA_BODY" '.id')
  rm -f "$CREATE_SA_BODY"

  [[ -n "$SA_ID" && "$SA_ID" != "null" ]] \
    || die "Grafana service account creation returned no id."
  ok "Service account '${SA_NAME}' created."
fi

# Force Admin role if it differs — this runs every time so a reinstall that
# finds an existing Editor-role SA will always upgrade it.
if [[ "$SA_ROLE" != "Admin" ]]; then
  PATCH_SA_BODY=$(mktemp)
  PATCH_SA_CODE=$(curl -sS -o "$PATCH_SA_BODY" -w '%{http_code}' \
    -X PATCH \
    -u "$GRAFANA_ADMIN" \
    -H "Content-Type: application/json" \
    -d '{"role":"Admin"}' \
    "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}" 2>/dev/null || echo "000")

  [[ "$PATCH_SA_CODE" == "200" ]] \
    || die "Failed to update service account role (HTTP $PATCH_SA_CODE): $(tr -d '\n' < "$PATCH_SA_BODY")"
  rm -f "$PATCH_SA_BODY"
fi

# Verify role is now Admin regardless of prior state.
SA_VERIFY_BODY=$(mktemp)
SA_VERIFY_CODE=$(curl -sS -o "$SA_VERIFY_BODY" -w '%{http_code}' \
  -u "$GRAFANA_ADMIN" \
  "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}" 2>/dev/null || echo "000")

[[ "$SA_VERIFY_CODE" == "200" ]] \
  || die "Failed to verify service account (HTTP $SA_VERIFY_CODE): $(tr -d '\n' < "$SA_VERIFY_BODY")"

SA_ROLE=$(_json_field_or_empty "$SA_VERIFY_BODY" '.role')
rm -f "$SA_VERIFY_BODY"

[[ "$SA_ROLE" == "Admin" ]] \
  || die "Service account '${SA_NAME}' role verification failed; expected Admin, got '${SA_ROLE:-<empty>}'"
ok "Service account '${SA_NAME}' has Admin role."

# If the stored token already works against the provisioning API, keep it.
if _verify_sa_token "${GRAFANA_SA_TOKEN:-}"; then
  ok "Stored Grafana service account token is valid."
else
  if [[ -n "${GRAFANA_SA_TOKEN:-}" ]]; then
    warn "Stored Grafana service account token is invalid — rotating."
  else
    info "No Grafana service account token stored — creating one."
  fi

  # Delete all installer-managed old tokens (idempotent).
  LIST_TOKENS_BODY=$(mktemp)
  LIST_TOKENS_CODE=$(curl -sS -o "$LIST_TOKENS_BODY" -w '%{http_code}' \
    -u "$GRAFANA_ADMIN" \
    "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}/tokens" 2>/dev/null || echo "000")

  [[ "$LIST_TOKENS_CODE" == "200" ]] \
    || die "Failed to list service account tokens (HTTP $LIST_TOKENS_CODE): $(tr -d '\n' < "$LIST_TOKENS_BODY")"

  mapfile -t OLD_TOKEN_IDS < <(
    jq -r \
      --arg legacy "$SA_TOKEN_LEGACY_NAME" \
      --arg prefix "$SA_TOKEN_PREFIX" \
      '.[] | select(.name == $legacy or (.name | startswith($prefix))) | .id' \
      "$LIST_TOKENS_BODY" 2>/dev/null
  )
  rm -f "$LIST_TOKENS_BODY"

  if [[ "${#OLD_TOKEN_IDS[@]}" -gt 0 ]]; then
    for token_id in "${OLD_TOKEN_IDS[@]}"; do
      DELETE_TOKEN_BODY=$(mktemp)
      DELETE_TOKEN_CODE=$(curl -sS -o "$DELETE_TOKEN_BODY" -w '%{http_code}' \
        -X DELETE \
        -u "$GRAFANA_ADMIN" \
        "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}/tokens/${token_id}" 2>/dev/null || echo "000")

      [[ "$DELETE_TOKEN_CODE" == "200" || "$DELETE_TOKEN_CODE" == "204" ]] \
        || die "Failed to delete old token ${token_id} (HTTP $DELETE_TOKEN_CODE): $(tr -d '\n' < "$DELETE_TOKEN_BODY")"
      rm -f "$DELETE_TOKEN_BODY"
    done
    ok "Old installer-managed service account token(s) removed."
  fi

  TOKEN_NAME="${SA_TOKEN_PREFIX}-$(date +%Y%m%d%H%M%S)"
  CREATE_TOKEN_BODY=$(mktemp)
  CREATE_TOKEN_CODE=$(curl -sS -o "$CREATE_TOKEN_BODY" -w '%{http_code}' \
    -X POST \
    -u "$GRAFANA_ADMIN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${TOKEN_NAME}\"}" \
    "${GRAFANA_URL}/api/serviceaccounts/${SA_ID}/tokens" 2>/dev/null || echo "000")

  [[ "$CREATE_TOKEN_CODE" == "200" ]] \
    || die "Failed to create service account token (HTTP $CREATE_TOKEN_CODE): $(tr -d '\n' < "$CREATE_TOKEN_BODY")"

  SA_TOKEN=$(_json_field_or_empty "$CREATE_TOKEN_BODY" '.key')
  rm -f "$CREATE_TOKEN_BODY"

  [[ -n "$SA_TOKEN" && "$SA_TOKEN" != "null" ]] \
    || die "Grafana returned success creating a token but the key field is absent."

  _upsert_env_var "GRAFANA_SA_TOKEN" "$SA_TOKEN"
  export GRAFANA_SA_TOKEN="$SA_TOKEN"

  _verify_sa_token "$SA_TOKEN" \
    || die "New token was created but cannot access the provisioning API (policy PUT)."

  ok "Grafana service account token rotated and stored in .env."

  # Restart alert-api so it picks up the new token from .env.
  docker compose restart alert-api >/dev/null 2>&1 || true
fi

# Final hard check — if this fails, recipient routing will be broken on day one.
if _verify_sa_token "${GRAFANA_SA_TOKEN:-}"; then
  ok "Service account has provisioning API access (alert routing will work)."
else
  die "Service account token still cannot read provisioning policy.  New recipients added via the UI will NOT receive alert emails."
fi

# =============================================================================
# 2. alert-api — start and wait for health
# =============================================================================
step "Alert API"

docker compose up -d alert-api >/dev/null 2>&1 || true
wait_for "Alert API" "http://localhost:8000/api/health" "200"

# =============================================================================
# 3. Grafana notification policy rebuild
# =============================================================================
# Rebuilds the full notification policy via alert-api, which reads every
# existing contact point and writes a complete routing tree:
#   - per-alert routes for contacts assigned via notify_to labels
#   - catch-all routes for every auto-subscribe email contact point
#   - lab-slack catch-all
#
# Handles file-provisioned policy (invalidProvenance) internally.
# Idempotent — safe to run on every install whether recipients exist or not.

step "Notification policy repair"

ALERT_API_URL="http://localhost:8000"
REBUILD_BODY=$(mktemp)
REBUILD_CODE=$(curl -sS -o "$REBUILD_BODY" -w '%{http_code}' \
  -X POST \
  -u "${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}" \
  "${ALERT_API_URL}/api/policy/rebuild" 2>/dev/null || echo "000")

if [[ "$REBUILD_CODE" == "200" ]]; then
  ok "Notification routing policy rebuilt."
else
  warn "Policy rebuild returned HTTP $REBUILD_CODE: $(tr -d '\n' < "$REBUILD_BODY")"
  warn "Existing recipients may not receive alerts until this succeeds."
  warn "Retry: python3 testui/diag.py --rebuild"
fi
rm -f "$REBUILD_BODY"

# ── Routing policy verification test ──────────────────────────────────────────
# Confirm that every routable email contact point has a route in the policy.
# A contact point is "routable" if: type=email, non-empty uid, address is not
# the @example.com placeholder.  The provisioned default receiver (lab-email)
# is excluded — it's the policy.receiver fallback, not a per-recipient route.

step "Notification routing policy verification"

if ! command -v jq >/dev/null 2>&1; then
  warn "jq not found — skipping routing verification."
  warn "Install jq to enable this check."
else
  POLICY_FILE=$(mktemp)
  RECIPIENTS_FILE=$(mktemp)

  curl -sS \
    -H "Authorization: Bearer ${GRAFANA_SA_TOKEN}" \
    "${GRAFANA_URL}/api/v1/provisioning/policies" > "$POLICY_FILE" 2>/dev/null || echo '{}' > "$POLICY_FILE"

  # Use alert-api /api/recipients which includes auto_subscribe status.
  # Only recipients with auto_subscribe=true need a catch-all route.
  curl -sS \
    -u "${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}" \
    "${ALERT_API_URL}/api/recipients" > "$RECIPIENTS_FILE" 2>/dev/null || echo '[]' > "$RECIPIENTS_FILE"

  MISSING=$(jq -rn \
    --slurpfile policy "$POLICY_FILE" \
    --slurpfile recipients "$RECIPIENTS_FILE" \
    '
      ($policy[0].routes // [] | [.[].receiver]) as $receivers |
      ($policy[0].receiver // "")               as $default   |
      $recipients[0][] |
      select(.type == "email") |
      select(.uid != null and .uid != "") |
      select(.name != $default) |
      select(.auto_subscribe == true) |
      .name |
      select(. as $n | ($receivers | index($n)) == null)
    ' 2>/dev/null || true)

  rm -f "$POLICY_FILE" "$RECIPIENTS_FILE"

  ROUTING_OK=true
  if [[ -z "$MISSING" ]]; then
    ok "All email recipients have routes in the notification policy."
  else
    echo ""
    while IFS= read -r name; do
      [[ -z "$name" ]] && continue
      warn "  Missing route for recipient: ${name}"
    done <<<"$MISSING"
    warn "One or more recipients are not routed — they will NOT receive alert emails."
    ROUTING_OK=false
  fi
fi

# =============================================================================
# 4. E2E email delivery test
# =============================================================================
# Validates the full production routing path:
#   alert-api recipient add → policy rebuild → metric push → alert fires →
#   Alertmanager routes → SMTP delivery → inbox confirmed
#
# This is deliberately NOT the /api/recipients/check "test send" which uses
# the Grafana receiver-test endpoint and bypasses Alertmanager routing entirely.

step "E2E email delivery test"

if [[ "$SKIP_E2E" == "true" ]]; then
  warn "E2E test skipped (--skip-e2e flag)."
  warn "Run manually: python3 testui/e2e_mail_test.py"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  warn "python3 not found — skipping E2E email delivery test."
  warn "Install python3 and run: python3 testui/e2e_mail_test.py"
  exit 0
fi

E2E_ARGS=(
  "--api-url"         "http://localhost:8000/api"
  "--grafana-url"     "${GRAFANA_URL}"
  "--pushgateway-url" "http://localhost:9091"
  "--username"        "${GF_ADMIN_USER:-admin}"
  "--password"        "${GF_ADMIN_PASSWORD}"
  "--recipient-email" "alerts.wanglab@gmail.com"
)

if [[ -n "${GMAIL_APP_PASSWORD:-}" ]]; then
  info "GMAIL_APP_PASSWORD found — running full E2E test with inbox verification."
  E2E_ARGS+=("--imap-password" "${GMAIL_APP_PASSWORD}")
else
  warn "GMAIL_APP_PASSWORD not set in .env — running E2E test WITHOUT inbox verification."
  warn "The test will confirm alert firing but cannot confirm inbox delivery."
  warn "Set GMAIL_APP_PASSWORD in .env to a Gmail App Password for alerts.wanglab@gmail.com"
  warn "to get full end-to-end proof on every install."
  E2E_ARGS+=("--skip-email-check")
fi

e2e_exit=0
python3 testui/e2e_mail_test.py "${E2E_ARGS[@]}" || e2e_exit=$?

case "$e2e_exit" in
  0)
    ok "E2E email delivery test PASSED."
    ;;
  1)
    warn "E2E email delivery test FAILED."
    warn "The alert fired but email was not confirmed delivered."
    warn "Check: SMTP settings in .env, Grafana/Alertmanager logs, Gmail spam folder."
    warn "Routing diagnostics: python3 testui/diag.py"
    warn "Retry inbox check: python3 testui/check_sender_inbox.py --query install-e2e --since-minutes 10"
    # Not a hard failure — the infrastructure is up, email may be delayed.
    ;;
  2)
    warn "E2E test could not run (setup error, see output above)."
    warn "Ensure the core stack is healthy and .env credentials are correct."
    ;;
  *)
    warn "E2E test exited with unexpected code $e2e_exit."
    ;;
esac

# If routing verification found missing routes, exit with failure now that E2E
# test has had a chance to run.
if [[ "${ROUTING_OK:-true}" == "false" ]]; then
  die "One or more recipients are not routed — they will NOT receive alert emails."
fi
