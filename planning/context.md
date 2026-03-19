# Project Context — for resuming in a new conversation

## What this project is

A monitoring stack for 3 dilution refrigerators in the Wang Lab.
- **Server** (`Fridge-server/`): Linux Docker Compose stack — receives fridge data, stores it, visualises it in Grafana, and sends alerts
- **Uploader** (`Fridge-data-uploader/`): Windows Python script running on each fridge's computer via Task Scheduler, reads Bluefors log files, pushes Prometheus metrics to Pushgateway every minute

## Fridges

| Name | Type | Status |
|---|---|---|
| Manny | Bluefors | Online, pushing data to a dead EC2 link. Will redirect to new server. |
| Sid | Bluefors | Online, pushing data. CH file parsing partially broken, different component set from Manny. Channel layout unknown — need collectall.py output. |
| Dodo | Oxford Instruments | No uploader written yet. Data format unknown. Deferred. |

## Server — current state after Phase A

The stack runs correctly in CI. Has not been deployed to the actual server yet (user is waiting on college IT for a Linux server; currently using a local Arch Linux machine as a stand-in).

### To deploy:
1. `cp .env.example .env` and fill in all values
2. `./install.sh`
3. Point Manny and Sid's `server.env` `PUSHGATEWAY_URL` at the new server IP

### Key .env values needed before deploy:
- `GF_ADMIN_PASSWORD` — set a real password
- `SMTP_PASSWORD` — SendGrid API key (user needs to sign up, verify zickers.us domain, generate key)
- `SLACK_WEBHOOK_URL` — user already has a Slack webhook (was configured before, needs cleanup)
- `DDNS_USERNAME` / `DDNS_TOKEN` — name.com account creds for DynDNS
- `ALLOWED_PUSH_CIDR` — college network CIDR (IT hasn't provided yet, leave empty for now)
- `LAB_USER_LOGIN` / `LAB_USER_PASSWORD` — for the Grafana Editor account for lab members

## Architecture decisions summary

- **Alerts**: Grafana unified alerting (UI) → Alertmanager (routing) → Slack + SendGrid email
- **Reverse proxy**: Caddy (auto-TLS from Let's Encrypt for zickers.us)
- **DynDNS**: ddclient → name.com API (domain will change when lab switches colleges)
- **Email**: SendGrid SMTP relay, no separate mail container
- **Grafana**: anonymous Viewer access enabled (for WordPress panel embedding); Editor lab user account
- **Dashboards**: `allowUiUpdates: true` — UI edits persist in grafana-data volume
- **Prometheus retention**: 30d / 15GB cap (env vars, adjustable)
- **Watchtower**: monitor-only, emails when image updates available

## Secret management pattern

- `config/alertmanager/alertmanager.yml.template` is in git
- `config/alertmanager/alertmanager.yml` is in git as a log-only default; install.sh overwrites it with real values from .env via envsubst — **do not commit after running install.sh**
- `config/ddclient/ddclient.conf` is gitignored; install.sh generates it from template
- `.env` is gitignored

## What to work on next

**Phase B** is the immediate next step — wiring up real alert routing:
1. Confirm SendGrid key is available in .env
2. Add Grafana unified alerting contact points provisioning file
3. Write a real fridge temperature alert rule in Grafana (e.g. CH6 MXC temp > 0.030 K)
4. Test alert delivery to Slack and email
5. Write `docs/managing-alerts.md`

**Phase C** is blocked on Sid's channel layout. To unblock:
- Commit the collectall.py fix (already done in working tree)
- Push to git, pull on Sid's machine, remote desktop to Sid, run collectall.ps1
- Paste the dpaste output URL into the conversation

## Important files to know

| File | Purpose |
|---|---|
| `docker-compose.yml` | All 7 services, profiles, volumes |
| `.env.example` | Template with all variables |
| `install.sh` | Entry point for all installs (idempotent, CI-aware) |
| `config/alertmanager/alertmanager.yml.template` | Alert routing config template |
| `config/caddy/Caddyfile` | Reverse proxy + TLS config |
| `Fridge-data-uploader/push_metrics.py` | Main Windows metrics collector |
| `Fridge-data-uploader/metric_metadata.py` | Maps raw Bluefors keys to Prometheus metric names |
| `Fridge-data-uploader/example_data/collectall.py` | Diagnostic tool — reads all files in today's Bluefors folder |

## Decisions still open

- `ALLOWED_PUSH_CIDR`: empty until IT provides the college network CIDR
- Sid fridge config: blocked on channel layout data
- Dodo (Oxford Instruments): data format unknown, deferred
- zickers.us domain: will change when lab switches colleges in ~1 year
- Rotating IP: noted, not addressed yet (ddclient handles it for the server)

## User profile

Physics lab (Wang Lab), setting up fridge monitoring. The PI will view Grafana dashboards — this is a key milestone. Lab has ~15 people. User has access to Slack workspace, name.com domain (zickers.us), WordPress lab website. Non-technical lab members need to manage alerts via Grafana UI.
