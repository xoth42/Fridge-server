# Phase B-2 — Alert API + Web Frontend (Sub-Agent Prompt)

## Objective

Build a stateless FastAPI service (`alert-api`) and a vanilla HTML/JS frontend
(`alert-ui/`) that let non-technical lab members create, view, and delete
threshold alerts through a simple web form. The API is a translation proxy to
Grafana's HTTP API — **no database**.

Authentication note:
Require entering the user/pass of an account with perms (Lab User/Admin). Use the grafana and prometheus system to do authentication. Still, the alerts will be a all-users->to-same-alerts system once auth is confirmed. 

## Prerequisites

Phase B-1 must be complete: provisioned baseline alerts, contact points, and
notification policy already exist in `config/grafana/provisioning/alerting/`.

## Workspace

Root: `/Users/zickers/Research/Chen_Wang_lab/Fridge-server`

## Key existing files you MUST read before writing code

| File | Why |
|---|---|
| `docker-compose.yml` | Understand existing services, networks (`fridge-monitoring`), volumes, and Grafana env vars. You will add a new service here. |
| `config/caddy/Caddyfile` | Understand current reverse proxy config. You will add routes here. |
| `config/caddy/Dockerfile` | Caddy uses a custom build (DNS-01 plugin). The alert-ui static files will be mounted into this container. |
| `.env.example` | Understand existing env vars. You will add new ones. |
| `install.sh` | Understand the install flow (especially sections 7-8: health checks and lab user creation). You will add service account creation here. |
| `config/grafana/provisioning/alerting/contact-points.yml` | Know the provisioned contact point UIDs (`cp-lab-email`, `cp-lab-slack`) so the API can reference them. |
| `config/grafana/provisioning/alerting/notification-policy.yml` | Understand the policy tree — the API must work within it, not override it. |
| `planning/alert-system.md` | Full design context and architecture diagram. |

---

## Implementation Status: COMPLETE (2026-04-13)

Phase B-2 has been implemented. The spec below reflects what was actually built,
which deviated from the original plan in the auth model (see note at top of file).

### Files created

| File | Purpose |
|---|---|
| `alert-api/Dockerfile` | python:3.12-slim, uvicorn on :8000 |
| `alert-api/requirements.txt` | fastapi, uvicorn, httpx, pyyaml, pydantic (pinned) |
| `alert-api/metrics.yml` | 18-metric allowlist + fridge/operator config |
| `alert-api/schemas.py` | Pydantic request/response models |
| `alert-api/grafana_client.py` | GrafanaClient: validate creds, CRUD rules, contact points, folders |
| `alert-api/main.py` | FastAPI routes + Grafana Basic-auth dependency |
| `alert-ui/index.html` | Single-page: login modal, alert table, create form, recipients panel |
| `alert-ui/style.css` | Responsive CSS, mobile-friendly |
| `alert-ui/app.js` | Fetch logic, 30s auto-refresh, sessionStorage auth |

### Files modified

| File | Change |
|---|---|
| `docker-compose.yml` | Added `alert-api` service; caddy `depends_on` + `./alert-ui:/srv/alert-ui:ro` |
| `config/caddy/Caddyfile` | `/alerts/api/*` → alert-api:8000, `/alerts/*` → file_server (no basicauth — auth is in the app) |
| `.env.example` | Added `GRAFANA_SA_TOKEN=` |
| `install.sh` | Alert-api health check (§7), service account creation (§8b), Alert Manager URL in summary |

### Auth design (deviation from original spec)

The original spec called for `ALERT_API_SECRET` bearer token + Caddy basicauth +
`LAB_USER_PASSWORD_HASH`. The implementation uses **Grafana Basic auth** instead:

- Frontend login modal collects Grafana username/password → `sessionStorage`
- Every API call sends `Authorization: Basic <base64>`
- `alert-api` validates credentials against `GET /api/user` on Grafana
- Write operations use `GRAFANA_SA_TOKEN` (Editor-role service account)
- No shared secrets, no Caddy basicauth, no `ALERT_API_SECRET`

### Key implementation details

- **PromQL injection prevention:** allowlist + `re.fullmatch(r"[a-z][a-z0-9_]*")` on metric names
- **XSS prevention:** `escHtml()` on all dynamic content
- **Path traversal prevention:** UID validated with `re.fullmatch(r"[a-zA-Z0-9_-]+")`
- **Provisioned rule protection:** `delete_alert_rule` checks `provenance == "file"` before delete
- **`X-Disable-Provenance: true`** on all POST/DELETE so user-created rules stay UI-editable
- **Notification routing:** via Grafana notification policy (by severity label), not per-rule binding. Notify checkboxes removed; info hint shown instead.
