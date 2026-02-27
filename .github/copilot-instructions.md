# Copilot Instructions

## Repository Overview

This repository contains infrastructure-as-code for a **Fridge monitoring server** built on top of the Prometheus observability stack. It targets Ubuntu 24.04 on AWS EC2.

## Stack

| Service | Port | Purpose |
|---|---|---|
| Prometheus | 9090 | Time-series metrics storage and alerting evaluation |
| Pushgateway | 9091 | Receives metrics pushed from fridge monitoring clients |
| Alertmanager | 9093 | Routes and deduplicates alerts (email / Slack / webhooks) |
| Grafana | 3000 | Dashboards and visualisation |

## Key Metrics

- **`fsmx`** — fridge temperature value pushed by the client via the Pushgateway.
  - Alert fires as `TemperatureTooHigh` when `fsmx > 350` for more than 2 minutes.
  - Alert fires as `TemperatureTooLow` when `fsmx < 50` for more than 2 minutes.
  - Alert fires as `MachineSilent` when no push has been received for more than 3 minutes.

## File Layout

```
old_server_setup.sh   # Idempotent Bash setup script (run as root via sudo)
.github/
  copilot-instructions.md
```

## Conventions

- The setup script uses `set -euo pipefail` — every command must succeed, variables must be set, and pipe failures are treated as errors.
- Services run under dedicated non-login system users (`prometheus`, `pushgateway`, `alertmanager`).
- All binaries are installed to `/opt/prometheus-stack/`.
- Configuration lives in `/etc/prometheus/` (including a `rules/` subdirectory for alert rule files).
- Prometheus data is stored in `/var/lib/prometheus/` with a 90-day retention period.
- All four services are managed by **systemd**.

## Client Integration

The Windows/.NET fridge client should set the environment variable:

```
PUSHGATEWAY_URL=<EC2-public-DNS>:9091
```

Metrics are pushed to `http://$PUSHGATEWAY_URL/metrics/job/<job_name>`.

## When Editing the Setup Script

- Pin all component versions explicitly (see `PROM_VERSION`, `PUSHGW_VERSION`, `ALERTMGR_VERSION`).
- Prefer `wget -q` for downloads to keep output clean.
- After modifying alert rules, validate with `promtool check rules /etc/prometheus/rules/*.yml`.
- After modifying `prometheus.yml`, validate with `promtool check config /etc/prometheus/prometheus.yml`.
- Alertmanager receiver credentials (email, Slack, etc.) must **never** be committed — use environment variables or a secrets manager.
