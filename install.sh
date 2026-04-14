#!/usr/bin/env bash
# =============================================================================
# install.sh — Fridge Monitoring Stack installer / updater
# =============================================================================
# Idempotent: safe to re-run after config changes or on an existing install.
# Also serves as the entry point for CI (GitHub Actions sets CI=true).
#
# Usage:
#   ./install.sh              — full production install
#   CI=true ./install.sh      — CI smoke-test install (skips TLS/DynDNS/firewall)
#
# Prerequisites (production):
#   - Docker with Compose plugin
#   - gettext (provides envsubst): apt install gettext  /  pacman -S gettext
#   - .env filled in from .env.example
# =============================================================================

set -euo pipefail

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

IS_CI="${CI:-false}"
[[ "$IS_CI" == "true" ]] && info "Running in CI mode — TLS, DynDNS, firewall and lab user steps skipped."

# =============================================================================
# 1. Preflight checks
# =============================================================================
step "Preflight"

command -v docker >/dev/null 2>&1 \
  || die "Docker is not installed. Install Docker Engine: https://docs.docker.com/engine/install/"

docker compose version >/dev/null 2>&1 \
  || die "Docker Compose plugin not found. Install with: apt install docker-compose-plugin  (or equivalent for your distro)"

command -v jq >/dev/null 2>&1 \
  || die "jq not found. Install jq:\n  Ubuntu/Debian: apt install jq\n  Arch:          pacman -S jq"

ok "Docker $(docker --version | awk '{print $3}' | tr -d ',') with Compose plugin found."
command -v envsubst >/dev/null 2>&1 \
  || die "envsubst not found. Install gettext:\n  Ubuntu/Debian: apt install gettext\n  Arch:          pacman -S gettext"

ok "envsubst found."
ok "jq found."
# =============================================================================
# 2. Environment file
# =============================================================================
step "Environment"

[[ -f .env ]] \
  || die ".env not found.\nCopy the template and fill in your values:\n  cp .env.example .env\n  \$EDITOR .env"

# Export all variables from .env so envsubst and this script can use them.
set -a
# shellcheck source=/dev/null
source .env
set +a

# Re-read variables that may contain special characters (e.g. '!') which
# Docker Compose's own .env parser strips. Bash sourcing above handles them
# correctly; we re-export here so Docker Compose picks up the shell env var
# (which takes precedence over its own .env parsing).
_reread_var() {
  local key="$1"
  local raw
  raw=$(grep -m1 "^${key}=" .env | cut -d= -f2- | sed "s/^['\"]//;s/['\"]$//")
  export "${key}=${raw}"
}
_reread_var NAMEDOTCOM_USER

# Validate required variables (stricter in production than CI).
REQUIRED_VARS_ALWAYS=(GF_ADMIN_PASSWORD)
REQUIRED_VARS_PROD=(DOMAIN SMTP_PASSWORD SMTP_FROM DUCKDNS_TOKEN DUCKDNS_SUBDOMAINS NAMEDOTCOM_USER NAMEDOTCOM_API_TOKEN)

for var in "${REQUIRED_VARS_ALWAYS[@]}"; do
  [[ -n "${!var:-}" ]] || die "Required variable '$var' is not set in .env"
done

if [[ "$IS_CI" != "true" ]]; then
  for var in "${REQUIRED_VARS_PROD[@]}"; do
    [[ -n "${!var:-}" ]] || warn "Variable '$var' is empty in .env — some features will not work."
  done
fi

ok "Environment loaded."

# =============================================================================
# 3. Generate configs from templates
# =============================================================================
step "Generating configs"

# ── Alertmanager ──────────────────────────────────────────────────────────────
# Set safe placeholder defaults so envsubst produces a valid YAML even when
# optional notification variables are not configured yet.
# Alertmanager will log delivery failures but will not fail to start.
export SMTP_HOST="${SMTP_HOST:-smtp.sendgrid.net}"
export SMTP_PORT="${SMTP_PORT:-587}"
export SMTP_USER="${SMTP_USER:-apikey}"
export SMTP_FROM="${SMTP_FROM:-alerts@example.com}"
export SMTP_PASSWORD="${SMTP_PASSWORD:-}"
export ALERT_EMAIL_TO="${ALERT_EMAIL_TO:-noreply@example.com}"
export SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-https://hooks.slack.com/services/NOTCONFIGURED/NOTCONFIGURED/notconfigured}"
export SLACK_CHANNEL="${SLACK_CHANNEL:-#fridge-alerts}"

envsubst < config/alertmanager/alertmanager.yml.template \
         > config/alertmanager/alertmanager.runtime.yml
ok "config/alertmanager/alertmanager.runtime.yml generated (untracked)."

# DuckDNS is configured entirely via env vars in docker-compose.yml — no file generation needed.

# =============================================================================
# 4. Firewall — restrict Pushgateway to trusted CIDR (production only)
# =============================================================================
if [[ "$IS_CI" != "true" ]]; then
  step "Firewall"
  if command -v ufw >/dev/null 2>&1; then
    if [[ -n "${ALLOWED_PUSH_CIDR:-}" ]]; then
      info "Restricting port 9091 (Pushgateway) to $ALLOWED_PUSH_CIDR"

      ufw_allow_rule_exists() {
        ufw status 2>/dev/null | awk -v cidr="$ALLOWED_PUSH_CIDR" '
          $1=="9091/tcp" && $2=="ALLOW" && $3==cidr { found=1 }
          END { exit(found ? 0 : 1) }
        '
      }

      ufw_deny_rule_exists() {
        ufw status 2>/dev/null | awk '
          ($1=="9091" || $1=="9091/tcp") && $2=="DENY" { found=1 }
          END { exit(found ? 0 : 1) }
        '
      }

      # Allow must be inserted before deny so it is evaluated first.
      if ufw insert 1 allow from "$ALLOWED_PUSH_CIDR" to any port 9091 proto tcp >/dev/null 2>&1; then
        ok "ufw: allow $ALLOWED_PUSH_CIDR → 9091"
      elif ufw_allow_rule_exists; then
        ok "ufw: allow $ALLOWED_PUSH_CIDR → 9091 already exists."
      else
        die "Failed to apply ufw allow rule for $ALLOWED_PUSH_CIDR on port 9091. Run install.sh with sufficient privileges and a valid ALLOWED_PUSH_CIDR."
      fi

      if ufw deny 9091 >/dev/null 2>&1; then
        ok "ufw: deny 9091"
      elif ufw_deny_rule_exists; then
        ok "ufw: deny 9091 already exists."
      else
        die "Failed to apply ufw deny 9091 rule. Pushgateway may be exposed."
      fi

      ok "Firewall configured."
    else
      warn "ALLOWED_PUSH_CIDR is not set — port 9091 (Pushgateway) is open to all."
      warn "Set ALLOWED_PUSH_CIDR in .env to your college network CIDR and re-run install.sh."
    fi
  else
    warn "ufw not found — skipping firewall setup. Secure port 9091 manually if needed."
  fi
fi

# =============================================================================
# 5. Pull images
# =============================================================================
step "Pulling images"

if [[ "$IS_CI" == "true" ]]; then
  docker compose pull
else
  docker compose --profile production pull
fi
ok "Images up to date."

# =============================================================================
# 6. Start (or restart updated) stack
# =============================================================================

if [[ "$IS_CI" == "true" ]]; then
  docker compose up -d prometheus alertmanager pushgateway grafana
else
  docker compose --profile production up -d prometheus alertmanager pushgateway grafana alert-api caddy duckdns watchtower
fi
ok "Core stack started."

# =============================================================================
# 7. Health checks
# =============================================================================
step "Health checks"

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

wait_for "Prometheus"   "http://localhost:9090/-/ready"   "200"
wait_for "Pushgateway"  "http://localhost:9091/-/healthy"  "200"
wait_for "Alertmanager" "http://localhost:9093/-/healthy"  "200"
wait_for "Grafana"      "http://localhost:3000/api/health" "200"

# =============================================================================
# 8. Create lab user account (production only)
# =============================================================================
if [[ "$IS_CI" != "true" ]] && [[ -n "${LAB_USER_LOGIN:-}" ]]; then
  step "Lab user"

  GRAFANA_ADMIN="${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}"
  EXISTING_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -u "$GRAFANA_ADMIN" \
    "http://localhost:3000/api/users/lookup?loginOrEmail=${LAB_USER_LOGIN}" \
    2>/dev/null || echo "000")

  if [[ "$EXISTING_CODE" == "404" ]]; then
    CREATE_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
      -X POST \
      -u "$GRAFANA_ADMIN" \
      -H "Content-Type: application/json" \
      -d "{
        \"name\":     \"${LAB_USER_NAME:-Lab User}\",
        \"email\":    \"${LAB_USER_EMAIL:-lab@example.com}\",
        \"login\":    \"${LAB_USER_LOGIN}\",
        \"password\": \"${LAB_USER_PASSWORD:-changeme}\",
        \"role\":     \"Editor\"
      }" \
      "http://localhost:3000/api/admin/users" 2>/dev/null || echo "000")

    [[ "$CREATE_CODE" == "200" ]] \
      && ok "Lab user '${LAB_USER_LOGIN}' created (Editor role)." \
      || warn "Lab user creation returned HTTP $CREATE_CODE — check Grafana logs."
  elif [[ "$EXISTING_CODE" == "200" ]]; then
    ok "Lab user '${LAB_USER_LOGIN}' already exists — skipping."
  else
    warn "Could not check lab user (HTTP $EXISTING_CODE) — skipping creation."
  fi
fi


# =============================================================================
# 8b. Grafana service account for alert-api
# =============================================================================
# Self-healing flow:
# - ensure service account exists
# - ensure role is Admin
# - verify stored token from .env
# - if token is missing/invalid, delete installer-managed old tokens, mint a new one,
#   replace GRAFANA_SA_TOKEN in .env, restart alert-api, and verify again
if [[ "$IS_CI" != "true" ]]; then
  step "Alert API service account"

  GRAFANA_ADMIN="${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}"
  GRAFANA_URL="http://localhost:3000"
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

  # Force Admin role if needed.
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

  # Verify role after create/patch.
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

  # If the stored token already works, keep it.
  if _verify_sa_token "${GRAFANA_SA_TOKEN:-}"; then
    ok "Stored Grafana service account token is valid."
  else
    if [[ -n "${GRAFANA_SA_TOKEN:-}" ]]; then
      warn "Stored Grafana service account token is invalid — rotating."
    else
      info "No Grafana service account token stored — creating one."
    fi

    # Delete installer-managed old tokens so creation is idempotent.
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
          || die "Failed to delete old service account token ${token_id} (HTTP $DELETE_TOKEN_CODE): $(tr -d '\n' < "$DELETE_TOKEN_BODY")"
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
      || die "Grafana returned success creating a service account token, but no token key was present."

    _upsert_env_var "GRAFANA_SA_TOKEN" "$SA_TOKEN"
    export GRAFANA_SA_TOKEN="$SA_TOKEN"

    _verify_sa_token "$SA_TOKEN" \
      || die "New Grafana service account token was created but cannot access the provisioning API."

    ok "Grafana service account token rotated and stored in .env."

    # Restart alert-api so it picks up the new token.
    docker compose restart alert-api >/dev/null 2>&1 || true
  fi

  # Final verification: this must be good or recipient routing will break.
  if _verify_sa_token "${GRAFANA_SA_TOKEN:-}"; then
    ok "Service account has provisioning API access (alert routing will work)."
  else
    die "Service account token still cannot read provisioning policy. New recipients added via the UI will NOT receive alert emails."
  fi

  # Start alert-api after token verification
  docker compose up -d alert-api >/dev/null 2>&1 || true
  wait_for "Alert API" "http://localhost:8000/api/health" "200"
fi

# =============================================================================
# 8c. Grafana notification policy bootstrap
# =============================================================================
# Sets the initial alert routing policy via API so timing and receivers are
# correct from day one.  The policy file (notification-policy.yml) is
# intentionally empty to avoid file-provenance locking; this step owns policy.
#
# Timing must match grafana_client.py rebuild_notification_policy():
#   group_wait:      10s   — delay before first notification fires
#   group_interval:   2m   — delay for follow-up batches within a group
#   repeat_interval:  4h   — re-notify interval for sustained alerts
#
# Only the two provisioned receivers (lab-slack, lab-email) are seeded here.
# Recipient-specific routes are added by the alert-api when users add contacts.
if [[ "$IS_CI" != "true" ]]; then
  step "Grafana notification policy"

  GRAFANA_ADMIN="${GF_ADMIN_USER:-admin}:${GF_ADMIN_PASSWORD}"
  POLICY_URL="http://localhost:3000/api/v1/provisioning/policies"

  INITIAL_POLICY='{
    "receiver": "lab-email",
    "group_by": [],
    "group_wait": "10s",
    "group_interval": "2m",
    "repeat_interval": "4h",
    "routes": [
      {"receiver": "lab-slack", "continue": true},
      {"receiver": "lab-email", "continue": true}
    ]
  }'

  _apply_policy() {
    curl -s -o /tmp/_policy_resp.json -w '%{http_code}' \
      -X PUT \
      -u "$GRAFANA_ADMIN" \
      -H "Content-Type: application/json" \
      -H "X-Disable-Provenance: true" \
      -d "$INITIAL_POLICY" \
      "$POLICY_URL" 2>/dev/null || echo "000"
  }

  HTTP_CODE=$(_apply_policy)

  if [[ "$HTTP_CODE" == "202" ]]; then
    ok "Grafana notification policy configured."
  elif grep -q "invalidProvenance" /tmp/_policy_resp.json 2>/dev/null; then
    # Existing policy is file-provisioned — reset it first, then retry.
    curl -s -X DELETE \
      -u "$GRAFANA_ADMIN" \
      -H "X-Disable-Provenance: true" \
      "$POLICY_URL" >/dev/null 2>&1 || true
    HTTP_CODE=$(_apply_policy)
    [[ "$HTTP_CODE" == "202" ]] \
      && ok "Grafana notification policy configured (after provenance reset)." \
      || warn "Notification policy setup returned HTTP $HTTP_CODE — run: python3 testui/diag.py --rebuild"
  else
    warn "Notification policy setup returned HTTP $HTTP_CODE — run: python3 testui/diag.py --rebuild"
  fi

  # # Verify the service account token has Admin access to the provisioning API.
  # # If this fails, adding recipients via the UI will silently fail to update
  # # routing — every new email address will not receive alerts.
  # if [[ -n "${GRAFANA_SA_TOKEN:-}" ]]; then
  #   SA_POLICY_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  #     -X GET \
  #     -H "Authorization: Bearer ${GRAFANA_SA_TOKEN}" \
  #     "$POLICY_URL" 2>/dev/null || echo "000")
  #   if [[ "$SA_POLICY_CODE" == "200" ]]; then
  #     ok "Service account has provisioning API access (alert routing will work)."
  #   else
  #     warn "Service account cannot read provisioning policy (HTTP $SA_POLICY_CODE)." \
  #          "New recipients added via the UI will NOT receive alert emails." \
  #          "Check that the 'alert-api' service account has Admin role in Grafana."
  #   fi
  # fi
fi

# =============================================================================
# 9. Summary
# =============================================================================
step "Done"

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     Fridge Monitoring Stack is Running       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
if [[ "$IS_CI" != "true" ]]; then
  echo -e "  ${BOLD}Grafana (public):${NC}    ${GRAFANA_PUBLIC_URL:-https://${DOMAIN}}"
  echo -e "  ${BOLD}Alert Manager:${NC}       ${GRAFANA_PUBLIC_URL:-https://${DOMAIN}}/alerts/"
fi
echo -e "  ${BOLD}Grafana (local):${NC}     http://localhost:3000"
echo -e "  ${BOLD}Pushgateway:${NC}         http://${HOST_IP}:9091"
echo -e "  ${BOLD}Prometheus:${NC}          http://localhost:9090"
echo -e "  ${BOLD}Alertmanager:${NC}        http://localhost:9093"
echo ""
echo -e "  ${BOLD}Send fridge metrics to:${NC}"
echo -e "    http://${HOST_IP}:9091"
echo -e "    (Set PUSHGATEWAY_URL in each fridge's server.env)"
echo ""
if [[ "$IS_CI" != "true" ]]; then
  echo -e "  ${BOLD}Grafana admin:${NC}    ${GF_ADMIN_USER:-admin} / [your .env password]"
  [[ -n "${LAB_USER_LOGIN:-}" ]] && \
    echo -e "  ${BOLD}Grafana lab user:${NC} ${LAB_USER_LOGIN} / [your .env password]"
  echo ""
  echo -e "  ${BOLD}To stop:${NC}  docker compose --profile production down"
  echo -e "  ${BOLD}To update config:${NC}  edit .env → re-run ./install.sh"
fi
echo ""
