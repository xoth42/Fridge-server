# Reference

## Configuration files

| Path | Role |
| --- | --- |
| `.env.example` | Template for deployment secrets and runtime options |
| `docker-compose.yml` | Container topology, ports, volumes, and environment |
| `config/prometheus/prometheus.yml` | Prometheus scrape config |
| `config/prometheus/alerts.yml` | Prometheus rule file |
| `config/grafana/provisioning/` | Grafana datasources, dashboards, contact points, policies, templates |
| `alert-api/metrics.yml` | Allowed fridges, metrics, units, operators, and custom PromQL expressions |
| `config/caddy/Caddyfile` | HTTPS reverse proxy for Grafana and the alert UI |
| `config/alertmanager/alertmanager.yml.template` | Source template for generated Alertmanager config |
| `alert-ui/` | Static custom alert-management frontend |
| `alert-api/` | FastAPI backend used by the alert UI and Slack command |

## Alert UI

The custom alert UI lives at `/alerts/`. It signs users in with Grafana username/password credentials and sends those credentials to `alert-api` as HTTP Basic auth.