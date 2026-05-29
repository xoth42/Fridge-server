# Operations

The live stack is defined in `docker-compose.yml`.

## Services

| Service | Purpose | Host access |
| --- | --- | --- |
| `prometheus` | Scrapes Pushgateway and stores metrics | `127.0.0.1:9090` |
| `pushgateway` | Receives metric pushes from fridge computers | `0.0.0.0:9091` |
| `grafana` | Dashboards, users, contact points, alert rules | `127.0.0.1:3000` |
| `alertmanager` | Prometheus Alertmanager for template-based routes | `127.0.0.1:9093` |
| `alert-api` | FastAPI proxy used by the custom alert UI | `127.0.0.1:8000` |
| `caddy` | Public HTTPS reverse proxy for Grafana and `/alerts/` | `0.0.0.0:8443` |
| `duckdns` | Keeps the dynamic DNS name updated | no published port |
| `watchtower` | Monitor-only container update emails | no published port |

## Common commands

```bash
./install.sh
docker compose down
docker compose restart grafana
docker compose up -d --build alert-api caddy
docker compose logs -f grafana
```

## Health checks

```bash
curl http://localhost:9090/-/ready
curl http://localhost:9091/-/healthy
curl http://localhost:9093/-/healthy
curl http://localhost:3000/api/health
curl http://localhost:8000/api/health
```