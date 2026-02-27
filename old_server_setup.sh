#!/usr/bin/env bash
# ==========================================================================
# Prometheus Stack Setup — Ubuntu 24.04 on EC2
# Run as: sudo bash server_setup.sh
# ==========================================================================
set -euo pipefail

PROM_VERSION="3.2.1"
PUSHGW_VERSION="1.11.0"
ALERTMGR_VERSION="0.28.1"
GRAFANA_VERSION="latest"   # apt repo handles this

INSTALL_DIR="/opt/prometheus-stack"
DATA_DIR="/var/lib/prometheus"
CONFIG_DIR="/etc/prometheus"

PUBLIC_DNS="ec2-3-16-30-195.us-east-2.compute.amazonaws.com"

echo "============================================"
echo " 1. Creating users and directories"
echo "============================================"

# Dedicated non-login users — one per service
for svc in prometheus pushgateway alertmanager; do
    id -u "$svc" &>/dev/null || useradd --no-create-home --shell /usr/sbin/nologin "$svc"
done

mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$CONFIG_DIR" "$CONFIG_DIR/rules"
chown prometheus:prometheus "$DATA_DIR"

echo "============================================"
echo " 2. Downloading binaries"
echo "============================================"

cd /tmp

# Prometheus
wget -q "https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz"
tar xzf "prometheus-${PROM_VERSION}.linux-amd64.tar.gz"
cp "prometheus-${PROM_VERSION}.linux-amd64/prometheus"  "$INSTALL_DIR/"
cp "prometheus-${PROM_VERSION}.linux-amd64/promtool"    "$INSTALL_DIR/"

# Pushgateway
wget -q "https://github.com/prometheus/pushgateway/releases/download/v${PUSHGW_VERSION}/pushgateway-${PUSHGW_VERSION}.linux-amd64.tar.gz"
tar xzf "pushgateway-${PUSHGW_VERSION}.linux-amd64.tar.gz"
cp "pushgateway-${PUSHGW_VERSION}.linux-amd64/pushgateway" "$INSTALL_DIR/"

# Alertmanager
wget -q "https://github.com/prometheus/alertmanager/releases/download/v${ALERTMGR_VERSION}/alertmanager-${ALERTMGR_VERSION}.linux-amd64.tar.gz"
tar xzf "alertmanager-${ALERTMGR_VERSION}.linux-amd64.tar.gz"
cp "alertmanager-${ALERTMGR_VERSION}.linux-amd64/alertmanager" "$INSTALL_DIR/"
mkdir -p /var/lib/alertmanager
chown alertmanager:alertmanager /var/lib/alertmanager

# Cleanup
rm -rf /tmp/prometheus-* /tmp/pushgateway-* /tmp/alertmanager-*

echo "============================================"
echo " 3. Writing config files"
echo "============================================"

# ---- Prometheus main config ----
cat > "$CONFIG_DIR/prometheus.yml" <<'EOF'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "rules/*.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["localhost:9093"]

scrape_configs:
  # Prometheus monitors itself
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  # Scrape the Pushgateway for machine metrics
  - job_name: "pushgateway"
    honor_labels: true
    static_configs:
      - targets: ["localhost:9091"]
EOF

# ---- Alert rules (fridge temperature example) ----
cat > "$CONFIG_DIR/rules/fridge_alerts.yml" <<'EOF'
groups:
  - name: fridge_alerts
    rules:
      # ----- Temperature alarms -----
      - alert: TemperatureTooHigh
        expr: fsmx > 350
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "HIGH TEMP on {{ $labels.instance }}: {{ $value }}"
          description: "fsmx has been above 350 for over 2 minutes."

      - alert: TemperatureTooLow
        expr: fsmx < 50
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "LOW TEMP on {{ $labels.instance }}: {{ $value }}"
          description: "fsmx has been below 50 for over 2 minutes."

      # ----- Dead machine detection -----
      - alert: MachineSilent
        expr: time() - last_push_timestamp_seconds > 180
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "No data from {{ $labels.instance }} for over 3 minutes"
EOF

# ---- Alertmanager config (placeholder — routes to a receiver) ----
cat > "$CONFIG_DIR/alertmanager.yml" <<'EOF'
# TODO: Replace with your real email / Slack / PagerDuty receiver
route:
  receiver: "default"
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: "default"
    webhook_configs: []
    # Example email:
    # email_configs:
    #   - to: "you@example.com"
    #     from: "alertmanager@example.com"
    #     smarthost: "smtp.example.com:587"
    #     auth_username: "alertmanager@example.com"
    #     auth_password: "password"
EOF

chown -R prometheus:prometheus "$CONFIG_DIR"

echo "============================================"
echo " 4. Creating systemd services"
echo "============================================"

# ---- Prometheus ----
cat > /etc/systemd/system/prometheus.service <<EOF
[Unit]
Description=Prometheus
Wants=network-online.target
After=network-online.target

[Service]
User=prometheus
Group=prometheus
Type=simple
ExecStart=${INSTALL_DIR}/prometheus \\
  --config.file=${CONFIG_DIR}/prometheus.yml \\
  --storage.tsdb.path=${DATA_DIR} \\
  --web.listen-address=:9090 \\
  --storage.tsdb.retention.time=90d
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ---- Pushgateway ----
cat > /etc/systemd/system/pushgateway.service <<EOF
[Unit]
Description=Prometheus Pushgateway
Wants=network-online.target
After=network-online.target

[Service]
User=pushgateway
Group=pushgateway
Type=simple
ExecStart=${INSTALL_DIR}/pushgateway \\
  --web.listen-address=:9091
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ---- Alertmanager ----
cat > /etc/systemd/system/alertmanager.service <<EOF
[Unit]
Description=Prometheus Alertmanager
Wants=network-online.target
After=network-online.target

[Service]
User=alertmanager
Group=alertmanager
Type=simple
ExecStart=${INSTALL_DIR}/alertmanager \\
  --config.file=${CONFIG_DIR}/alertmanager.yml \\
  --storage.path=/var/lib/alertmanager
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "============================================"
echo " 5. Installing Grafana"
echo "============================================"

apt-get install -y apt-transport-https software-properties-common
wget -qO- https://apt.grafana.com/gpg.key | gpg --dearmor -o /usr/share/keyrings/grafana-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/grafana-archive-keyring.gpg] https://apt.grafana.com stable main" \
    > /etc/apt/sources.list.d/grafana.list
apt-get update -qq
apt-get install -y grafana

echo "============================================"
echo " 6. Enabling and starting all services"
echo "============================================"

systemctl daemon-reload
for svc in prometheus pushgateway alertmanager grafana-server; do
    systemctl enable "$svc"
    systemctl start  "$svc"
done

echo ""
echo "============================================"
echo " 7. Validating"
echo "============================================"

sleep 3
for pair in "prometheus:9090" "pushgateway:9091" "alertmanager:9093" "grafana-server:3000"; do
    svc="${pair%%:*}"
    port="${pair##*:}"
    if systemctl is-active --quiet "$svc"; then
        echo "  ✓  $svc  is running  (port $port)"
    else
        echo "  ✗  $svc  FAILED — check: journalctl -u $svc"
    fi
done

echo ""
echo "============================================"
echo " DONE — Open these ports in your EC2"
echo " Security Group (us-east-2):"
echo "============================================"
echo ""
echo "  Port   Service          URL"
echo "  ----   -------          ---"
echo "  9090   Prometheus       http://${PUBLIC_DNS}:9090"
echo "  9091   Pushgateway      http://${PUBLIC_DNS}:9091"
echo "  9093   Alertmanager     http://${PUBLIC_DNS}:9093"
echo "  3000   Grafana          http://${PUBLIC_DNS}:3000"
echo ""
echo "  Grafana default login:  admin / admin"
echo ""
echo "  Your Windows .env should have:"
echo "  PUSHGATEWAY_URL=${PUBLIC_DNS}:9091"
echo ""