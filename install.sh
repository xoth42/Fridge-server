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

command -v envsubst >/dev/null 2>&1 \
  || die "envsubst not found. Install gettext:\n  Ubuntu/Debian: apt install gettext\n  Arch:          pacman -S gettext"

ok "Docker $(docker --version | awk '{print $3}' | tr -d ',') with Compose plugin found."
ok "envsubst found."

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

# Validate required variables (stricter in production than CI).
REQUIRED_VARS_ALWAYS=(GF_ADMIN_PASSWORD)
REQUIRED_VARS_PROD=(DOMAIN SMTP_PASSWORD SMTP_FROM DDNS_USERNAME DDNS_TOKEN DDNS_HOSTNAME)

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
         > config/alertmanager/alertmanager.yml
ok "config/alertmanager/alertmanager.yml generated."

# ── ddclient (production only) ────────────────────────────────────────────────
if [[ "$IS_CI" != "true" ]]; then
  export DDNS_USERNAME="${DDNS_USERNAME:-NOTSET}"
  export DDNS_TOKEN="${DDNS_TOKEN:-NOTSET}"
  export DDNS_HOSTNAME="${DDNS_HOSTNAME:-${DOMAIN:-localhost}}"

  mkdir -p config/ddclient
  envsubst < config/ddclient/ddclient.conf.template \
           > config/ddclient/ddclient.conf
  ok "config/ddclient/ddclient.conf generated."
fi

# =============================================================================
# 4. Firewall — restrict Pushgateway to trusted CIDR (production only)
# =============================================================================
if [[ "$IS_CI" != "true" ]]; then
  step "Firewall"
  if command -v ufw >/dev/null 2>&1; then
    if [[ -n "${ALLOWED_PUSH_CIDR:-}" ]]; then
      info "Restricting port 9091 (Pushgateway) to $ALLOWED_PUSH_CIDR"
      ufw allow from "$ALLOWED_PUSH_CIDR" to any port 9091 proto tcp >/dev/null 2>&1 \
        && ok "ufw: allow $ALLOWED_PUSH_CIDR → 9091" \
        || warn "ufw rule may already exist — skipping."
      ufw deny 9091 >/dev/null 2>&1 || true
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
step "Starting stack"

if [[ "$IS_CI" == "true" ]]; then
  docker compose up -d
else
  docker compose --profile production up -d
fi
ok "Stack started."

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
