# fridge-server (phase 1)

Minimal containerized monitoring stack for physics lab fridges.

Phase 1 intentionally includes only:
- Prometheus
- Alertmanager
- Pushgateway
- Grafana
- A tiny synthetic metric push script

No custom web UI or reverse proxy is included yet.

## Prerequisites

- Docker
- Docker Compose (`docker compose` plugin or `docker-compose` binary)
- Python 3 (for test metric script)

## Quickstart

```bash
./install.sh
docker compose up -d
python3 testdata/pushtestmetrics.py --pushgateway-url http://localhost:9091
```

Then open Grafana:
- http://localhost:3000
- Login (local/dev **only**): `admin` / `admin`
- Open dashboard: **Fridge Test**

**Security note:** The `admin` / `admin` credentials are provided **only** for local development and CI smoke tests. Do **not** use these credentials in any non-dev deployment or on a network-accessible host. For any non-dev environment, override the Grafana admin password (for example by setting `GF_SECURITY_ADMIN_PASSWORD` via environment variables or a Docker Compose override file) before exposing port 3000.

## What gets provisioned

- Prometheus scrape targets:
  - `pushgateway:9091`
  - `alertmanager:9093`
- Prometheus alert rule:
  - `FridgeSyntheticMetricHigh` when `fridgetestmetric > 42` for 15s
- Alertmanager default route/receiver (`log-only`)
- Grafana datasource:
  - `Prometheus` -> `http://prometheus:9090`
- Grafana dashboard:
  - `Fridge Test`

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on all pushes and pull requests and does a full stack smoke test:
1. Starts all containers with Docker Compose.
2. Waits for Prometheus and Grafana readiness.
3. Pushes synthetic fridge metrics to Pushgateway.
4. Verifies Prometheus query API returns `fridgetestmetric`.
5. Verifies Grafana datasource and dashboard are provisioned.
6. Tears the stack down.
