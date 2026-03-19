# Phase A — Foundation Notes

## Decisions locked in

| Topic | Decision |
|---|---|
| Reverse proxy | Caddy (auto-TLS via Let's Encrypt, native DOMAIN env var) |
| HTTPS cert | Caddy handles automatically; no manual certbot step |
| DynDNS | ddclient (linuxserver) with name.com dyndns2 endpoint |
| Email delivery | SendGrid SMTP relay (smtp.sendgrid.net:587, user=apikey, pass=API key) |
| Mail containers | None — alertmanager + grafana connect directly to SendGrid SMTP |
| Alert rule owner | Grafana unified alerting (UI create/toggle/silence); Alertmanager routes to Slack+email |
| Container updates | Watchtower notify-only (monitor mode), weekly Monday 4am check |
| Dashboard persistence | `allowUiUpdates: true` — UI edits saved to grafana-data volume (SQLite) |
| Grafana anon access | Enabled, Viewer role — needed for WordPress panel embedding |
| Grafana embedding | GF_SECURITY_ALLOW_EMBEDDING=true — removes X-Frame-Options block |
| Lab user account | Editor role, created via Grafana API in install.sh (skipped in CI) |
| Config secrets | alertmanager.yml.template → alertmanager.yml via envsubst in install.sh |
| Compose profiles | `production` profile: caddy + ddclient (not started in CI) |
| CI env | CI creates .env with test values before running install.sh |

## Service list (docker-compose)

| Service | Image (pinned) | Profile | Port |
|---|---|---|---|
| prometheus | prom/prometheus:v2.54.1 | default | 9090 |
| pushgateway | prom/pushgateway:v1.9.0 | default | 9091 |
| alertmanager | prom/alertmanager:v0.27.0 | default | 9093 |
| grafana | grafana/grafana:10.4.5 | default | 3000 |
| caddy | caddy:2.8.4 | production | 80, 443 |
| ddclient | lscr.io/linuxserver/ddclient:3.11.2 | production | — |
| watchtower | containrrr/watchtower:1.7.1 | default | — |

## Generated files (not in git, created by install.sh)
- `config/alertmanager/alertmanager.yml` — from template + envsubst
- `config/ddclient/ddclient.conf` — from template + envsubst

## Open issues / deferred
- ALLOWED_PUSH_CIDR is empty (college IT hasn't provided CIDR yet); port 9091 open until set
- Domain zickers.us will change when lab switches colleges — note in docs
- ddclient version pinning: linuxserver/ddclient:3.11.2 — check for updates periodically
- alertmanager.yml is still committed (log-only default); install.sh overwrites it locally;
  user must NOT commit after running install.sh (run `git checkout config/alertmanager/alertmanager.yml` to restore)
- prometheus alerts.yml still has the synthetic test rule — clean up in Phase B

## Grafana anonymous access for WordPress embedding
- GF_AUTH_ANONYMOUS_ENABLED=true (Viewer role)
- GF_SECURITY_ALLOW_EMBEDDING=true
- Caddy config removes X-Frame-Options header so Grafana can set its own
- Panel URL format: https://zickers.us/d-solo/{dashUID}/{slug}?panelId={id}&orgId=1&refresh=30s
- Documented in docs/panel-embedding.md (Phase E)

## install.sh idempotency
- envsubst overwrites generated files each run (fine)
- ufw rules: check before adding
- `docker compose up -d`: idempotent by design
- Lab user: checks via API before creating
- Safe to re-run after config changes

## Phase B (alerts) pre-work done in Phase A
- alertmanager.yml.template has basic Slack + email receivers (Phase B will tune formatting)
- Grafana SMTP env vars wired up
- alertmanager template uses ${SLACK_WEBHOOK_URL} with placeholder fallback so alertmanager
  starts even if Slack not configured yet

## Fridge uploader (Phase C) — deferred
- Need Sid channel info (run collectall.py via remote desktop on Sid machine)
- collectall.py in Fridge-data-uploader/example_data/ works as-is for data collection
- Per-fridge config: fridge_configs/manny.config, sid.config, dodo.config (YAML format)
- push_metrics.py redesign to be config-driven

## Dashboard (Phase D) — deferred
- Log-scale Y axis for temperature panels (50K → 10mK range)
- Fridges: Manny (Bluefors), Sid (Bluefors), Dodo (Oxford Instruments — placeholder)
- Dashboards: fridge-overview (cross-fridge temps), pressures, per-fridge detail x3
- Need live data from real fridges to tune queries; do after fridge pointed at server

## Pushgateway stateless gap note
- On server restart, all Pushgateway metrics are lost until next push (~1 min gap)
- This is a known Pushgateway limitation, not fixable without switching to pull model
- Document in troubleshooting.md
