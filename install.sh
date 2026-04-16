#!/usr/bin/env bash
# =============================================================================
# install.sh — Fridge Monitoring Stack installer / updater
# =============================================================================
# Idempotent: safe to re-run after config changes or on an existing install.
#
# Usage:
#   ./install.sh
#
# Prerequisites:
#   - Docker with Compose plugin
#   - gettext (provides envsubst): apt install gettext  /  pacman -S gettext
#   - jq: apt install jq  /  pacman -S jq
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
REQUIRED_VARS_PROD=(DOMAIN SMTP_FROM DUCKDNS_TOKEN DUCKDNS_SUBDOMAINS NAMEDOTCOM_USER NAMEDOTCOM_API_TOKEN)

for var in "${REQUIRED_VARS_ALWAYS[@]}"; do
  [[ -n "${!var:-}" ]] || die "Required variable '$var' is not set in .env"
done

for var in "${REQUIRED_VARS_PROD[@]}"; do
  [[ -n "${!var:-}" ]] || warn "Variable '$var' is empty in .env — some features will not work."
done

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
# 4. Firewall — restrict Pushgateway to trusted CIDR
# =============================================================================
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

# =============================================================================
# 5. Pull and build images
# =============================================================================
step "Pulling images"

docker compose pull
ok "Images up to date."

# Build all locally-defined services (alert-api, caddy).  Always rebuilds so
# code changes are picked up on re-runs without needing --build or docker build.
step "Building local images"
docker compose build
ok "Local images built."

# =============================================================================
# 6. Start (or restart updated) stack
# =============================================================================

docker compose up -d
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
# 8. Create lab user account
# =============================================================================
if [[ -n "${LAB_USER_LOGIN:-}" ]]; then
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
# 8b. Alert UI setup (SA, policy, alert-api, E2E test)
# =============================================================================
step "Alert UI setup"
bash "${SCRIPT_DIR}/install_alert_ui.sh"

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
echo -e "  ${BOLD}Grafana (public):${NC}    ${GRAFANA_PUBLIC_URL:-https://${DOMAIN}}"
echo -e "  ${BOLD}Alert Manager:${NC}       ${GRAFANA_PUBLIC_URL:-https://${DOMAIN}}/alerts/"
echo -e "  ${BOLD}Grafana (local):${NC}     http://localhost:3000"
echo -e "  ${BOLD}Pushgateway:${NC}         http://${HOST_IP}:9091"
echo -e "  ${BOLD}Prometheus:${NC}          http://localhost:9090"
echo -e "  ${BOLD}Alertmanager:${NC}        http://localhost:9093"
echo ""
echo -e "  ${BOLD}Send fridge metrics to:${NC}"
echo -e "    http://${HOST_IP}:9091"
echo -e "    (Set PUSHGATEWAY_URL in each fridge's server.env)"
echo ""
echo -e "  ${BOLD}Grafana admin:${NC}    ${GF_ADMIN_USER:-admin} / [your .env password]"
[[ -n "${LAB_USER_LOGIN:-}" ]] && \
  echo -e "  ${BOLD}Grafana lab user:${NC} ${LAB_USER_LOGIN} / [your .env password]"
echo ""
echo -e "  ${BOLD}To stop:${NC}  docker compose down"
echo -e "  ${BOLD}To update config:${NC}  edit .env → re-run ./install.sh"
echo ""
