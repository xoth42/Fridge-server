#!/usr/bin/env bash
# Pre-deployment validation — run locally before pushing to the server.
# Usage: bash testdata/validate_before_deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
PASS=0; FAIL=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo -e "${GREEN}OK${NC}  $label"
    PASS=$((PASS+1))
  else
    echo -e "${RED}ERR${NC} $label"
    FAIL=$((FAIL+1))
  fi
}

echo "=== 1. Docker Compose config ==="
check "docker-compose.yml syntax" docker compose config --quiet

echo ""
echo "=== 2. YAML syntax (provisioning + prometheus + metrics) ==="
for f in \
  config/grafana/provisioning/alerting/contact-points.yml \
  config/grafana/provisioning/alerting/notification-policy.yml \
  config/grafana/provisioning/alerting/rules/manny-alerts.yml \
  config/grafana/provisioning/alerting/rules/dodo-alerts.yml \
  config/grafana/provisioning/alerting/rules/common-alerts.yml \
  config/prometheus/alerts.yml \
  alert-api/metrics.yml
do
  check "$f" python3 -c "import yaml; yaml.safe_load(open('$f'))"
done

echo ""
echo "=== 3. Python syntax (alert-api) ==="
for f in alert-api/main.py alert-api/grafana_client.py alert-api/schemas.py; do
  check "$f" python3 -m py_compile "$f"
done

echo ""
echo "=== 4. Datasource UID consistency ==="
# The hardcoded UID in grafana_client.py must match the provisioned datasource.
DS_UID=$(python3 -c "import yaml; print(yaml.safe_load(open('config/grafana/provisioning/datasources/prometheus.yml'))['datasources'][0]['uid'])")
CODE_UID=$(grep 'PROMETHEUS_DS_UID *=' alert-api/grafana_client.py | sed 's/.*= *"\([^"]*\)".*/\1/')
if [[ "$DS_UID" == "$CODE_UID" ]]; then
  check "datasource UID matches ($DS_UID)" true
else
  echo -e "${RED}ERR${NC} datasource UID mismatch: provisioning=$DS_UID code=$CODE_UID"
  FAIL=$((FAIL+1))
fi

echo ""
echo "=== 5. Caddyfile route order ==="
# /alerts/api/* must appear before /alerts/* which must appear before the grafana catch-all
API_LINE=$(grep -n 'handle /alerts/api' config/caddy/Caddyfile | head -1 | cut -d: -f1)
UI_LINE=$(grep -n 'handle /alerts/' config/caddy/Caddyfile | grep -v api | head -1 | cut -d: -f1)
GRAFANA_LINE=$(grep -n 'reverse_proxy grafana' config/caddy/Caddyfile | head -1 | cut -d: -f1)
if [[ -n "$API_LINE" && -n "$UI_LINE" && -n "$GRAFANA_LINE" ]] && \
   (( API_LINE < UI_LINE )) && (( UI_LINE < GRAFANA_LINE )); then
  check "Caddy route order: api($API_LINE) < ui($UI_LINE) < grafana($GRAFANA_LINE)" true
else
  echo -e "${RED}ERR${NC} Caddy route order wrong: api=$API_LINE ui=$UI_LINE grafana=$GRAFANA_LINE"
  FAIL=$((FAIL+1))
fi

echo ""
echo "==============================="
echo -e "Passed: ${GREEN}${PASS}${NC}  Failed: ${RED}${FAIL}${NC}"
if (( FAIL > 0 )); then
  echo -e "${RED}Fix errors before deploying.${NC}"
  exit 1
else
  echo -e "${GREEN}Ready to deploy.${NC}"
fi
