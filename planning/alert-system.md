# Alert System — Implementation Plan

## Chosen Design: Hybrid (Provisioning + Stateless API + Web Frontend)

### Why pure provisioning is insufficient

Grafana's provisioning docs state: **"You cannot edit provisioned resources from
files in Grafana."** Provisioned alert rules are read-only in the UI. This is
fine for baseline rules that should never be accidentally deleted, but it blocks
non-technical lab members from creating, editing, or removing their own alerts.

The lab needs both:

1. **Immutable baseline alerts** (provisioned YAML) — critical rules that survive
   volume loss, are version-controlled in git, and can't be accidentally disabled.
2. **User-managed alerts** (Grafana API) — created through a simple web form by
   lab members without SSH access, stored in Grafana's database, editable in the
   Grafana UI.

These coexist cleanly. Grafana treats provisioned and API-created alerts as
separate: provisioned rules are marked with a "provisioned" badge, API-created
rules are fully editable.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Caddy (existing)                                       │
│                                                         │
│  /alerts/*     → static HTML/JS frontend (alert-ui/)    │
│  /alerts/api/* → alert-api container (FastAPI)          │
│  /*            → grafana (existing)                     │
└───────┬─────────────────────────┬───────────────────────┘
        │                         │
        ▼                         ▼
┌───────────────┐     ┌───────────────────────────┐
│  alert-api    │────▶│  Grafana HTTP API          │
│  (stateless)  │     │  (service account token)   │
│  No database  │     └───────────────────────────┘
└───────────────┘               ▲
                                │ reads on startup
                 ┌──────────────┴──────────────┐
                 │  Provisioned YAML files      │
                 │  (baseline alerts, contacts) │
                 │  config/grafana/provisioning/ │
                 │  alerting/                    │
                 └─────────────────────────────┘
```

### Two tiers of alerts

| Tier | Stored where | Managed by | Editable in UI | Survives volume loss |
|---|---|---|---|---|
| **Baseline** (provisioned) | YAML files in git | You (admin) | No (read-only badge) | Yes |
| **User-created** | Grafana SQLite DB | Web frontend or Grafana UI | Yes | No (backed up via Grafana export) |

**Baseline alerts** (provisioned):
- MXC temperature > 30 mK (per fridge)
- Data staleness > 10 minutes (fridge stopped pushing)
- Compressor high-side pressure anomalies

**User-created alerts** (via web frontend):
- Experiment-specific thresholds ("alert me when CH2 > 5 K this week")
- Individual notification preferences

---

## Part 1: Provisioned Baseline Alerts

### Directory structure

```
config/grafana/provisioning/alerting/
├── contact-points.yml        # Lab-wide email + Slack contacts
├── notification-policy.yml   # Routing: severity → contact groups
└── rules/
    ├── manny-alerts.yml      # Manny baseline alert rules
    ├── dodo-alerts.yml       # Dodo baseline alert rules
    └── common-alerts.yml     # Cross-fridge (data staleness)
```

All files are checked into git. Secrets use Grafana's native `$VARIABLE`
interpolation (Grafana reads env vars directly — no envsubst needed).

### contact-points.yml

```yaml
apiVersion: 1

contactPoints:
  - orgId: 1
    name: lab-email
    receivers:
      - uid: cp-lab-email
        type: email
        settings:
          addresses: $ALERT_EMAIL_TO
          singleEmail: true

  - orgId: 1
    name: lab-slack
    receivers:
      - uid: cp-lab-slack
        type: slack
        settings:
          url: $SLACK_WEBHOOK_URL
          title: |-
            [{{ $$labels.severity | toUpper }}] {{ $$labels.alertname }}
          text: |-
            *Fridge:* {{ $$labels.fridge }}
            *Value:*  {{ $$values.A }}
            {{ $$annotations.summary }}
```

Note: `$$labels` and `$$values` are escaped so Grafana's provisioning engine
does not try to interpolate them as env vars. They become `$labels` and
`$values` in the stored template, which Grafana's alert engine then evaluates
at fire time.

### notification-policy.yml

```yaml
apiVersion: 1

policies:
  - orgId: 1
    receiver: lab-email
    group_by: ['alertname', 'fridge']
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    routes:
      - receiver: lab-slack
        continue: true
        matchers:
          - severity =~ "critical|warning"
      - receiver: lab-email
        matchers:
          - severity =~ "critical|warning"
```

> **WARNING:** Grafana treats the notification policy tree as a single resource.
> Provisioning it **overwrites** any UI-created policies. All routing must be
> defined in this one file. User-created alerts route through the same tree
> (matched by labels).

### Example rule file: manny-alerts.yml

```yaml
apiVersion: 1

groups:
  - orgId: 1
    name: manny-baseline
    folder: Baseline Alerts
    interval: 60s
    rules:
      - uid: manny-mxc-high
        title: "Manny — MXC Temperature High"
        condition: C
        data:
          - refId: A
            relativeTimeRange: { from: 300, to: 0 }
            datasourceUid: P1809F7CD0C75ACF3
            model:
              expr: ch6_t_kelvin{instance="fridge-manny"}
              intervalMs: 1000
              maxDataPoints: 43200
          - refId: C
            relativeTimeRange: { from: 300, to: 0 }
            datasourceUid: __expr__
            model:
              type: threshold
              expression: A
              conditions:
                - evaluator: { type: gt, params: [0.030] }
        noDataState: Alerting
        execErrState: Alerting
        for: 5m
        labels:
          severity: critical
          fridge: manny
          tier: baseline
        annotations:
          summary: "Manny MXC temp is {{ $$values.A }} K (threshold: 0.030 K)"
```

### Example rule file: common-alerts.yml

```yaml
apiVersion: 1

groups:
  - orgId: 1
    name: data-staleness
    folder: Baseline Alerts
    interval: 60s
    rules:
      - uid: staleness-all-fridges
        title: "Fridge Data Stale (>10 min)"
        condition: C
        data:
          - refId: A
            relativeTimeRange: { from: 900, to: 0 }
            datasourceUid: P1809F7CD0C75ACF3
            model:
              expr: (time() - last_push_timestamp_seconds{job="sensor_data"}) / 60
              intervalMs: 1000
              maxDataPoints: 43200
          - refId: C
            relativeTimeRange: { from: 900, to: 0 }
            datasourceUid: __expr__
            model:
              type: threshold
              expression: A
              conditions:
                - evaluator: { type: gt, params: [10] }
        noDataState: Alerting
        execErrState: Alerting
        for: 5m
        labels:
          severity: warning
          tier: baseline
        annotations:
          summary: "No data from {{ $$labels.instance }} for {{ $$values.A }} minutes"
```

### Provisioning integration with `install.sh`

No envsubst step needed for alerting provisioning files. Grafana natively
interpolates `$VARIABLE` from its own environment (set via docker-compose env
vars sourced from `.env`). The files are mounted read-only at:

```
./config/grafana/provisioning:/etc/grafana/provisioning:ro
```

This already works — the existing dashboard and datasource provisioning uses
the same mount. Adding files under `provisioning/alerting/` is all that's
needed.

### Provisioning gotcha: `$` escaping in annotations

Grafana's provisioning engine interpolates `$VARIABLE` at file-load time, but
alert annotations and `model` fields are **excluded from interpolation**
according to the docs. In practice, however, the `$$` escaping for template
variables (`$labels`, `$values`) in non-excluded fields (like contact point
settings) is mandatory. Use `$$labels` in provisioning YAML anywhere outside
`annotations` and `model`. Inside `annotations`, plain `$values` works but
`$$values` is safer for forward-compatibility.

### Prometheus alerts.yml transition

The current `config/prometheus/alerts.yml` has a synthetic test rule
(`FridgeSyntheticMetricHigh`). This fires through Prometheus → Alertmanager,
which is a separate path from Grafana unified alerting.

**Plan:**
- Keep `alerts.yml` for infrastructure-level rules only (e.g., Prometheus scrape
  target down), where alerting must work even if Grafana is unavailable.
- Remove `FridgeSyntheticMetricHigh` when real Grafana alerts are deployed.
- Do **not** duplicate fridge-specific alerts in both paths.

---

## Part 2: Web Frontend + Stateless API

### alert-api service (FastAPI, stateless)

A thin translation proxy between a simple web form and Grafana's HTTP API. No
database — Grafana is the sole source of truth.

#### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Checks Grafana API connectivity |
| `GET` | `/api/metrics` | Returns the allowed metric list (from config) |
| `GET` | `/api/alerts` | Queries Grafana, returns simplified alert list |
| `POST` | `/api/alerts` | Creates an alert rule + contact routing in Grafana |
| `DELETE` | `/api/alerts/{uid}` | Deletes an alert from Grafana (rejects if provisioned) |
| `GET` | `/api/recipients` | Lists contact points from Grafana |
| `POST` | `/api/recipients` | Creates a new email contact point in Grafana |

#### POST /api/alerts request body

```json
{
  "name": "Manny CH2 too warm",
  "fridge": "manny",
  "metric": "ch2_t_kelvin",
  "operator": ">",
  "threshold": 5.0,
  "for_duration": "5m",
  "severity": "warning",
  "recipient_uids": ["cp-lab-email"]
}
```

The backend:
1. Validates `fridge` against known instances (`fridge-manny`, `fridge-dodo`).
2. Validates `metric` against an allowlist loaded from a config file —
   **no raw PromQL accepted**.
3. Builds the full Grafana alert rule JSON (same structure as provisioned YAML
   but via the HTTP API).
4. Tags the rule with label `managed_by: alert-api` so it can be identified.
5. Creates or reuses notification policy routing for the specified recipients.
6. Returns `{ "uid": "...", "title": "..." }` or an error.

#### Metric allowlist (config file: `alert-api/metrics.yml`)

```yaml
metrics:
  - name: ch1_t_kelvin
    label: "CH1 50K Flange (K)"
    unit: K
  - name: ch2_t_kelvin
    label: "CH2 4K Flange (K)"
    unit: K
  - name: ch5_t_kelvin
    label: "CH5 Still (K)"
    unit: K
  - name: ch6_t_kelvin
    label: "CH6 MXC (K)"
    unit: K
  - name: ch9_t_kelvin
    label: "CH9 CP Flange (K)"
    unit: K
    fridges: [manny]          # only present on Manny
  - name: flowmeter_mmol_per_s
    label: "Flow Rate (mmol/s)"
    unit: mmol/s
  - name: maxigauge_ch1_pressure_mbar
    label: "Maxigauge CH1 (mbar)"
    unit: mbar
  # ... remaining maxigauge channels, compressor metrics, etc.

fridges:
  - id: fridge-manny
    label: Manny
  - id: fridge-dodo
    label: Dodo

operators:
  - { symbol: ">",  grafana_type: gt }
  - { symbol: "<",  grafana_type: lt }
  - { symbol: ">=", grafana_type: gte }
  - { symbol: "<=", grafana_type: lte }
```

This file is the **single place** to update when a new fridge or metric is
added. The frontend reads it via `GET /api/metrics` to populate dropdowns.

#### Authentication

- All `/api/*` endpoints require `Authorization: Bearer <token>`.
- The token is a shared secret set in `.env` as `ALERT_API_SECRET`.
- The frontend stores the token in a JavaScript variable (page is behind Caddy
  basicauth, so the token is not publicly exposed).
- The backend validates the token on every request before forwarding to Grafana.

This is two layers of auth: Caddy basicauth protects the page, bearer token
protects the API. Neither is sufficient alone, but together they cover:
- External access: blocked by Caddy basicauth.
- Internal access (other processes on server): blocked by bearer token.

#### Grafana service account

The API backend authenticates to Grafana using a service account token with
minimum required permissions:

```
- alerting.rules:read
- alerting.rules:write
- alerting.provisioning:read
- alerting.notifications:read
- alerting.notifications:write
```

Created via Grafana UI or API during `install.sh`:

```bash
# Create service account (in install.sh, after Grafana is running)
SA_ID=$(curl -s -X POST http://admin:${GF_ADMIN_PASSWORD}@localhost:3000/api/serviceaccounts \
  -H 'Content-Type: application/json' \
  -d '{"name":"alert-api","role":"Editor"}' | jq -r '.id')

SA_TOKEN=$(curl -s -X POST http://admin:${GF_ADMIN_PASSWORD}@localhost:3000/api/serviceaccounts/${SA_ID}/tokens \
  -H 'Content-Type: application/json' \
  -d '{"name":"alert-api-token"}' | jq -r '.key')

# Store for alert-api container
echo "GRAFANA_SA_TOKEN=${SA_TOKEN}" >> .env
```

#### Code structure

```
alert-api/
├── Dockerfile
├── requirements.txt      # fastapi, uvicorn, httpx, pyyaml
├── metrics.yml           # Metric/fridge allowlist
├── main.py               # FastAPI app, route handlers (~100 lines)
├── grafana_client.py     # GrafanaClient class: CRUD alert rules (~120 lines)
├── schemas.py            # Pydantic request/response models (~40 lines)
└── tests/
    └── test_alerts.py    # Integration tests against Grafana
```

Total: ~300 lines of application code.

#### Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Static frontend (alert-ui/)

Vanilla HTML + CSS + JS. No build step, no npm, no framework. Served as static
files by Caddy.

```
alert-ui/
├── index.html            # Single page with all views
├── style.css             # Minimal styling
└── app.js                # Fetch calls to /alerts/api/*
```

#### UI sections

1. **Alert list** — table showing all alerts (name, fridge, metric, threshold,
   status, tier badge: "baseline" or "custom"). Provisioned alerts show a lock
   icon, no delete button. User-created alerts show a delete button.

2. **Create alert form** — dropdowns populated from `GET /api/metrics`:
   - Fridge (dropdown)
   - Metric (dropdown, filtered by selected fridge)
   - Operator (dropdown: >, <, >=, <=)
   - Threshold (number input)
   - Severity (dropdown: warning, critical)
   - Recipients (multi-select from existing contact points)

3. **Recipients panel** — list of contact points. "Add email" button opens
   inline form (name + email address).

The entire frontend is one HTML file with embedded CSS and a `<script>` tag
that pulls `app.js`. No routing, no SPA framework, no transpilation.

### Caddy integration

Add routes to the existing Caddyfile:

```
{$DOMAIN} {
    # ... existing TLS and header config ...

    # Alert management UI — behind basicauth
    handle /alerts/api/* {
        reverse_proxy alert-api:8000
    }

    handle /alerts/* {
        root * /srv/alert-ui
        file_server
    }

    # Existing Grafana proxy (must be last / most general)
    reverse_proxy grafana:3000

    # ... existing header and log config ...
}
```

The `alert-ui/` directory is mounted into the Caddy container:

```yaml
# In docker-compose.yml, caddy service volumes:
volumes:
  - ./config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
  - ./alert-ui:/srv/alert-ui:ro
  - caddy_data:/data
  - caddy_config:/config
```

Caddy basicauth protects both `/alerts/*` and `/alerts/api/*`. The lab user
login (already planned in `.env` as `LAB_USER_LOGIN`/`LAB_USER_PASSWORD`) can
be reused here.

### docker-compose.yml addition

```yaml
  alert-api:
    build: ./alert-api
    container_name: fridge-alert-api
    environment:
      - GRAFANA_URL=http://grafana:3000
      - GRAFANA_SA_TOKEN=${GRAFANA_SA_TOKEN}
      - ALERT_API_SECRET=${ALERT_API_SECRET}
    ports:
      - "127.0.0.1:8000:8000"
    depends_on:
      - grafana
    networks:
      - fridge-monitoring
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/api/health').raise_for_status()"]
      interval: 30s
      timeout: 5s
      retries: 3
```

### New .env variables

```bash
# Alert API service
GRAFANA_SA_TOKEN=         # Grafana service account token (created by install.sh)
ALERT_API_SECRET=         # Shared bearer token for API auth (generate with: openssl rand -hex 32)
```

---

## Part 3: Differences from Alertmanager Path

The current `config/prometheus/alerts.yml` defines Prometheus-evaluated rules
(`FridgeSyntheticMetricHigh`) that fire through Prometheus → Alertmanager →
Slack/email. This is a separate alert pipeline from Grafana unified alerting.

Grafana provisioned alerts and API-created alerts both use **Grafana's built-in
alerting engine**, which routes through Grafana's own contact points.

**Rule: do not duplicate alerts in both paths.**

| Alert type | Pipeline | When to use |
|---|---|---|
| Infrastructure (scrape target down, Prometheus disk full) | Prometheus → Alertmanager | Must fire even if Grafana is unavailable |
| Fridge-specific (temperature, pressure, staleness) | Grafana unified alerting | Primary path for all fridge alerts |

Migration: remove `FridgeSyntheticMetricHigh` from `alerts.yml` when real
Grafana alerts are deployed. Keep `alerts.yml` for infrastructure rules only.

---

## Part 4: Rollout Plan

### Phase B-1: Provisioned baseline alerts (no new services)

This is independently deployable and provides immediate value.

1. Create `config/grafana/provisioning/alerting/` directory.
2. Write `contact-points.yml` — lab-wide email + Slack contacts.
3. Write `notification-policy.yml` — route by severity.
4. Write `rules/manny-alerts.yml` — MXC temp high.
5. Write `rules/common-alerts.yml` — data staleness.
6. Test: `docker compose up -d`, verify rules appear in Grafana UI with
   "provisioned" badge. Trigger a test alert by pushing a metric above
   threshold via Pushgateway.
7. Verify Slack and email delivery.
8. Remove `FridgeSyntheticMetricHigh` from `config/prometheus/alerts.yml`.
9. Add `rules/dodo-alerts.yml` once Dodo metrics are confirmed.
10. Commit and push — CI validates Grafana starts cleanly.

**Blocked on:** SendGrid API key in `.env` (for email delivery).
**Not blocked on:** Web frontend (baseline alerts work without it).

### Phase B-2: Alert API + frontend (new service)

Depends on Phase B-1 being complete and stable.

1. Create `alert-api/` directory with FastAPI app, `metrics.yml`, Dockerfile.
2. Write `grafana_client.py` — create/list/delete alert rules via Grafana API.
3. Write `main.py` — route handlers with bearer token auth.
4. Write `schemas.py` — Pydantic models for request validation.
5. Create `alert-ui/` with `index.html`, `style.css`, `app.js`.
6. Add `alert-api` service to `docker-compose.yml`.
7. Update Caddyfile with `/alerts/*` routes and basicauth.
8. Add `GRAFANA_SA_TOKEN` and `ALERT_API_SECRET` to `.env.example`.
9. Update `install.sh` to create the Grafana service account and token.
10. Write integration tests: create alert → verify in Grafana → delete → verify
    gone.
11. Test full flow: open `https://fridge.zickers.us:8443/alerts/`, log in with
    lab credentials, create an alert, see it fire.
12. Document in `docs/managing-alerts.md`.

### Phase B-3: Hardening

1. Pin all Python deps with version + hash in `requirements.txt`.
2. Add Watchtower monitoring for the `alert-api` container image.
3. Add a CI job that builds the alert-api image and runs integration tests
   against a test Grafana instance.
4. Write a backup script that exports all Grafana alert rules to JSON
   (covers user-created alerts that aren't in git).
5. Document recovery procedure: if `grafana-data` volume is lost, provisioned
   alerts auto-restore; user-created alerts are restored from the backup JSON.

---

## Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| Grafana volume lost | Provisioned alerts auto-restore. User-created alerts lost. | Scheduled JSON export backup (Phase B-3). |
| Grafana API changes in upgrade | alert-api may break (create/delete fail). | Pin Grafana image version. Test before upgrading. alert-api health check fails → Watchtower notifies. |
| Bearer token leak | Attacker can create/delete user alerts (not provisioned ones). | Rotate token. Caddy basicauth as second layer. Scoped service account limits blast radius. |
| Notification policy overwrite | Provisioning the policy tree overwrites any UI-created policies. | All routing defined in `notification-policy.yml`. Document: do not edit policies in Grafana UI. |
| Provisioning YAML syntax error | Grafana fails to start. | CI validates: start Grafana, check health endpoint, fail build if unhealthy. |
| alert-api container down | Existing alerts keep firing (they live in Grafana). Cannot create new alerts until container recovers. | Docker restart policy + health check. Not a data-loss scenario. |

---

## Decision Summary

| Criterion | Provisioned baseline | API + frontend | Both (hybrid) |
|---|---|---|---|
| New services | 0 | 1 | 1 |
| New databases | 0 | 0 | 0 |
| Self-service for lab | No (SSH required) | Yes | Yes |
| Critical alerts survive volume loss | Yes | No | Yes (baseline tier) |
| Git audit trail | Yes | No (use JSON backup) | Partial |
| Implementation effort | Small | Medium | Medium (additive) |

**Chosen: Hybrid.** Phase B-1 (provisioning) ships first with zero new
infrastructure. Phase B-2 (API + frontend) adds self-service when ready. Each
phase is independently useful and the second can be deferred without risk.

---

## Known Technical Debt

- **httpx.AsyncClient connection pooling:** `grafana_client.py` creates a new
  `httpx.AsyncClient` context manager per method call (no connection reuse).
  Fine for low-traffic lab use. If request volume grows, refactor to a single
  persistent client created in `lifespan()` and shared across requests. The
  change is straightforward — store the client as an instance attribute instead
  of using `async with` in every method.
