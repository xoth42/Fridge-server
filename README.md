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
- Login: `admin` / `admin`
- Open dashboard: **Fridge Test**

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

GitHub Actions (`.github/workflows/ci.yml`) runs on pushes/PRs to `main` and does a full stack smoke test:
1. Starts all containers with Docker Compose.
2. Waits for Prometheus and Grafana readiness.
3. Pushes synthetic fridge metrics to Pushgateway.
4. Verifies Prometheus query API returns `fridgetestmetric`.
5. Verifies Grafana datasource and dashboard are provisioned.
6. Tears the stack down.
