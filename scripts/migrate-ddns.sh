#!/usr/bin/env bash
# =============================================================================
# migrate-ddns.sh — swap DuckDNS container for qmcgaw/ddns-updater (name.com).
# =============================================================================
# One-shot migration. Idempotent — safe to re-run; the steps each detect
# their own "already done" state and skip.
#
# Run on EACH host (OLD source and NEW VPS) after pulling the repo updates.
# Order doesn't matter; both hosts run the same flow against the same name.com
# record. Whichever host the DNS A record currently points at "wins" until the
# next update cycle.
#
# What it does:
#   1. Sanity-check env (NAMEDOTCOM_USER/TOKEN, DDNS_DOMAIN, DDNS_OWNER)
#   2. Render config/ddns-updater/config.json from template
#   3. Inspect name.com for any stale CNAME at ${DDNS_OWNER}.${DDNS_DOMAIN}
#      that would block an A record — offer to delete it via the API
#   4. Stop + remove the old duckdns container (if present)
#   5. Start the new ddns-updater container
#   6. Tail its logs briefly to confirm first update succeeded
#
# Usage:
#   sudo -E ./scripts/migrate-ddns.sh
#   # add --yes to auto-confirm the name.com CNAME deletion
#   # add --skip-namecom-check to skip the CNAME inspection entirely
# =============================================================================
set -Eeuo pipefail

C_RED='\033[0;31m'; C_GRN='\033[0;32m'; C_YEL='\033[1;33m'
C_BLU='\033[0;34m'; C_BLD='\033[1m';    C_NC='\033[0m'
info() { echo -e "${C_BLU}[INFO]${C_NC}  $*"; }
ok()   { echo -e "${C_GRN}[ OK ]${C_NC}  $*"; }
warn() { echo -e "${C_YEL}[WARN]${C_NC}  $*"; }
die()  { echo -e "${C_RED}[FAIL]${C_NC}  $*" >&2; exit 1; }
step() { echo -e "\n${C_BLD}── $* ──${C_NC}"; }

ASSUME_YES=0
SKIP_NAMECOM=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y)              ASSUME_YES=1 ;;
    --skip-namecom-check)  SKIP_NAMECOM=1 ;;
    -h|--help)
      sed -n '/^# Usage/,/^# ====/p' "$0" | sed 's/^# //;s/^#//'
      exit 0 ;;
    *) die "unknown arg: $arg" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# ─── 1. preflight ────────────────────────────────────────────────────────────
step "Preflight"
for bin in docker envsubst jq curl; do
  command -v "$bin" >/dev/null || die "$bin not in PATH"
done
[[ -f .env ]] || die ".env not found at $REPO_DIR/.env"
[[ -f docker-compose.yml ]] || die "docker-compose.yml not in $REPO_DIR"
[[ -f config/ddns-updater/config.json.template ]] \
  || die "config/ddns-updater/config.json.template missing — pull the repo first?"

# Load .env without leaking values to logs.
set -a
# shellcheck disable=SC1091
source .env
set +a

for v in NAMEDOTCOM_USER NAMEDOTCOM_API_TOKEN DDNS_DOMAIN DDNS_OWNER; do
  [[ -n "${!v:-}" ]] || die "$v is empty in .env — set it before running this script"
done
ok "env loaded: ${DDNS_OWNER}.${DDNS_DOMAIN}, user=${NAMEDOTCOM_USER}"

# ─── 2. render config.json ───────────────────────────────────────────────────
step "Render ddns-updater config"
mkdir -p config/ddns-updater
envsubst < config/ddns-updater/config.json.template \
         > config/ddns-updater/config.json
chmod 600 config/ddns-updater/config.json
ok "config/ddns-updater/config.json rendered (mode 0600)"

# Validate the rendered JSON before handing it to the container.
jq -e . config/ddns-updater/config.json >/dev/null \
  || die "rendered config.json is not valid JSON"

# ─── 3. name.com CNAME check ─────────────────────────────────────────────────
NAMECOM_API="https://api.name.com/v4/domains/${DDNS_DOMAIN}/records"
nc_auth=(-u "${NAMEDOTCOM_USER}:${NAMEDOTCOM_API_TOKEN}")
fqdn="${DDNS_OWNER}.${DDNS_DOMAIN}"

if (( SKIP_NAMECOM )); then
  step "name.com CNAME check — skipped (--skip-namecom-check)"
else
  step "name.com record inspection"
  info "fetching record list for ${DDNS_DOMAIN} via name.com API…"
  resp="$(curl -fsS "${nc_auth[@]}" "$NAMECOM_API")" \
    || die "name.com API call failed (auth? token? CIDR allowlist on name.com?)"

  cname_id="$(echo "$resp" | jq -r \
    --arg fq "$fqdn" --arg owner "$DDNS_OWNER" '
      .records[]?
      | select(.type=="CNAME" and (.fqdn==$fq or .host==$owner))
      | .id' \
    | head -1)"
  a_id="$(echo "$resp" | jq -r \
    --arg fq "$fqdn" --arg owner "$DDNS_OWNER" '
      .records[]?
      | select(.type=="A" and (.fqdn==$fq or .host==$owner))
      | .id' \
    | head -1)"

  if [[ -n "$cname_id" ]]; then
    warn "found stale CNAME record id=$cname_id at $fqdn — blocks A record creation"
    if (( ASSUME_YES )); then
      confirm=y
    else
      read -rp "Delete the CNAME via name.com API now? [y/N] " confirm
    fi
    if [[ "$confirm" =~ ^[Yy] ]]; then
      curl -fsS -X DELETE "${nc_auth[@]}" "${NAMECOM_API}/${cname_id}" >/dev/null \
        && ok "CNAME deleted"
    else
      warn "skipping — ddns-updater will fail to create an A record until you delete the CNAME at name.com"
    fi
  else
    ok "no stale CNAME for $fqdn"
  fi
  if [[ -n "$a_id" ]]; then
    ok "existing A record id=$a_id at $fqdn — ddns-updater will update it in place"
  else
    info "no existing A record — ddns-updater will create one on first run"
  fi
fi

# ─── 4. drop the old duckdns container ───────────────────────────────────────
step "Stop and remove old duckdns container"
if docker compose ps -a --services 2>/dev/null | grep -qx duckdns; then
  docker compose stop duckdns 2>/dev/null || true
  docker compose rm -f duckdns 2>/dev/null || true
  ok "duckdns service removed from running stack"
elif docker ps -a --format '{{.Names}}' | grep -qx fridge-duckdns; then
  docker rm -f fridge-duckdns >/dev/null
  ok "stale fridge-duckdns container removed"
else
  ok "no duckdns container present"
fi

# ─── 5. start ddns-updater ───────────────────────────────────────────────────
step "Start ddns-updater"
# --remove-orphans cleans up the now-dead fridge-duckdns container if it
# survived a partial earlier run (compose stop+rm misses anything not
# referenced by the current compose file).
docker compose up -d --remove-orphans ddns-updater
sleep 2

if ! docker ps --format '{{.Names}}' | grep -qx fridge-ddns-updater; then
  die "fridge-ddns-updater failed to start (docker compose logs ddns-updater)"
fi
ok "fridge-ddns-updater is running"

# ─── 6. confirm first update ─────────────────────────────────────────────────
step "Verify first update"
info "waiting 10s for the first poll…"
sleep 10

logs="$(docker compose logs ddns-updater --tail=80 --no-color 2>/dev/null)"
echo "$logs" | tail -20 | sed 's/^/    /'

if echo "$logs" | grep -qiE 'failed|error|invalid'; then
  warn "ddns-updater log contains errors — review above. Common causes:"
  warn "  - CNAME still present at name.com (delete it first)"
  warn "  - NAMEDOTCOM_API_TOKEN wrong"
  warn "  - DDNS_OWNER doesn't match an existing record AND name.com auto-create blocked"
elif echo "$logs" | grep -qiE 'success|updated|up to date'; then
  ok "ddns-updater is healthy"
else
  warn "could not confirm success from logs — re-check in a minute with:"
  warn "  docker compose logs ddns-updater --tail=80"
fi

# ─── done ────────────────────────────────────────────────────────────────────
cat <<EOF

── Next ─────────────────────────────────────────────────────────────────────
  1. Wait for DNS propagation (TTL=300s in config.json → max ~5 min)
  2. Verify from outside:
       dig +short ${fqdn}
     Should return this host's public IP: $(curl -fsS ifconfig.me 2>/dev/null || echo '<unknown>')
  3. Caddy is unaffected — it issues certs via DNS-01 against the same
     name.com API, regardless of A vs CNAME at the leaf.

The old DuckDNS subdomain (if any) is now orphaned. You can let it expire
or delete it from your DuckDNS account; nothing in the stack reads it.
EOF
ok "migration complete"
