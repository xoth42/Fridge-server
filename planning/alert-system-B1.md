# Phase B-1 — Provisioned Baseline Alerts (Sub-Agent Prompt)

## Objective

Create Grafana alerting provisioning YAML files that deploy baseline alert
rules, contact points, and a notification policy. These are immutable
(read-only in Grafana UI), version-controlled, and survive volume loss.

No new services or containers are added. Only new YAML files under the existing
`config/grafana/provisioning/` mount.

## Workspace

Root: `/Users/zickers/Research/Chen_Wang_lab/Fridge-server`

## Key existing files you MUST read before editing

| File | Why |
|---|---|
| `docker-compose.yml` | Verify Grafana env vars and provisioning mount path |
| `config/grafana/provisioning/datasources/prometheus.yml` | Get the Prometheus datasource UID (`P1809F7CD0C75ACF3`) |
| `.env.example` | Understand available env vars (`ALERT_EMAIL_TO`, `SLACK_WEBHOOK_URL`, `SLACK_CHANNEL`) |
| `config/prometheus/alerts.yml` | Contains `FridgeSyntheticMetricHigh` — must be cleaned up |
| `config/alertmanager/alertmanager.yml.template` | Understand existing Alertmanager routing (separate pipeline) |

## Tasks (in order)

### 1. Create directory structure

```
config/grafana/provisioning/alerting/
config/grafana/provisioning/alerting/rules/
```

### 2. Create `config/grafana/provisioning/alerting/contact-points.yml`

Two contact points:

- **lab-email** — type: `email`, uid: `cp-lab-email`
  - `addresses: $ALERT_EMAIL_TO` (Grafana interpolates this from its own env)
  - `singleEmail: true`

- **lab-slack** — type: `slack`, uid: `cp-lab-slack`
  - `url: $SLACK_WEBHOOK_URL`
  - Include title and text templates using `$$labels`, `$$values`, `$$annotations`
    (double-dollar escaping required so Grafana's provisioning engine doesn't
    interpolate them as env vars)

Template for Slack:
```
title: |-
  [{{ $$labels.severity | toUpper }}] {{ $$labels.alertname }}
text: |-
  *Fridge:* {{ $$labels.fridge }}
  *Value:*  {{ $$values.A }}
  {{ $$annotations.summary }}
```

### 3. Create `config/grafana/provisioning/alerting/notification-policy.yml`

Single policy tree (Grafana requires the entire tree in one file — provisioning
**overwrites** any UI-created policies):

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

### 4. Create `config/grafana/provisioning/alerting/rules/manny-alerts.yml`

Provisioned baseline rule for Manny MXC temperature:

- **UID:** `manny-mxc-high` (max 40 chars, letters/numbers/hyphen/underscore)
- **Title:** `Manny — MXC Temperature High`
- **Folder:** `Baseline Alerts`
- **Evaluation interval:** `60s`
- **Data query** (refId: A):
  - datasourceUid: `P1809F7CD0C75ACF3`
  - relativeTimeRange: `{ from: 300, to: 0 }`
  - model.expr: `ch6_t_kelvin{instance="fridge-manny"}`
  - model.intervalMs: 1000
  - model.maxDataPoints: 43200
- **Threshold condition** (refId: C):
  - datasourceUid: `__expr__`
  - model.type: `threshold`
  - model.expression: `A`
  - evaluator: `{ type: gt, params: [0.030] }`
- **condition field:** `C`
- **for:** `5m`
- **noDataState:** `Alerting`
- **execErrState:** `Alerting`
- **labels:** `severity: critical`, `fridge: manny`, `tier: baseline`
- **annotations:**
  - summary: `Manny MXC temp is {{ $$values.A }} K (threshold: 0.030 K)`

The annotation field is **excluded from env var interpolation** per Grafana docs,
so `$$values` is used for safety/forward-compat but `$values` would also work
inside annotations specifically.

### 5. Create `config/grafana/provisioning/alerting/rules/dodo-alerts.yml`

Same structure as manny-alerts.yml but for Dodo:

- **UID:** `dodo-mxc-high`
- **Title:** `Dodo — MXC Temperature High`
- model.expr: `ch6_t_kelvin{instance="fridge-dodo"}`
- labels: `fridge: dodo`
- annotations reference Dodo

**Dodo differences from Manny:**
- Dodo has temperature channels: CH1, CH2, CH5, CH6 (NO CH9)
- Instance label: `fridge-dodo`

### 6. Create `config/grafana/provisioning/alerting/rules/common-alerts.yml`

Data staleness alert (cross-fridge):

- **UID:** `staleness-all-fridges`
- **Title:** `Fridge Data Stale (>10 min)`
- **Folder:** `Baseline Alerts`
- model.expr: `(time() - last_push_timestamp_seconds{job="sensor_data"}) / 60`
- relativeTimeRange: `{ from: 900, to: 0 }`
- Threshold: `> 10` (minutes)
- **for:** `5m`
- **noDataState:** `Alerting`
- labels: `severity: warning`, `tier: baseline` (no `fridge` label — the instance label comes from the query result)
- annotation summary: `No data from {{ $$labels.instance }} for {{ $$values.A }} minutes`

### 7. Clean up `config/prometheus/alerts.yml`

Replace the `FridgeSyntheticMetricHigh` rule with a comment placeholder for
future infrastructure-level rules. The file should look like:

```yaml
groups:
  # Infrastructure-level alert rules evaluated by Prometheus.
  # Fridge-specific alerts are managed via Grafana unified alerting
  # (see config/grafana/provisioning/alerting/).
  []
```

This keeps the file valid YAML (Prometheus expects it) while removing the
synthetic test rule. The `alerts.yml` file is referenced in the Prometheus
command config (`--config.file`), and the file itself is mounted in
docker-compose.yml at `/etc/prometheus/alerts.yml:ro`.

**Important:** In `config/prometheus/prometheus.yml`, check if there's a
`rule_files:` section referencing `alerts.yml`. If so, keep the file. If
`alerts.yml` is listed in `rule_files`, the file must remain valid YAML
even if empty of rules.

### 8. Verify no install.sh changes needed

Grafana provisioning files require **no envsubst step**. Grafana natively
interpolates `$VARIABLE` from its container environment. The existing
provisioning mount in docker-compose.yml already covers the alerting
subdirectory:

```yaml
volumes:
  - ./config/grafana/provisioning:/etc/grafana/provisioning:ro
```

No changes to install.sh or docker-compose.yml are needed for B-1.

## Validation criteria

After all files are created, an agent or human should be able to:

1. Run `docker compose up -d` (with a valid `.env`)
2. Open Grafana at `localhost:3000`
3. Navigate to Alerting → Alert rules
4. See a "Baseline Alerts" folder with:
   - `Manny — MXC Temperature High`
   - `Dodo — MXC Temperature High`
   - `Fridge Data Stale (>10 min)`
5. Each rule shows a "provisioned" badge (not editable in UI)
6. Navigate to Alerting → Contact points
7. See `lab-email` and `lab-slack` contact points
8. Navigate to Alerting → Notification policies
9. See the provisioned policy tree with Slack + email routing

## Files created/modified (summary)

| Action | File |
|---|---|
| CREATE | `config/grafana/provisioning/alerting/contact-points.yml` |
| CREATE | `config/grafana/provisioning/alerting/notification-policy.yml` |
| CREATE | `config/grafana/provisioning/alerting/rules/manny-alerts.yml` |
| CREATE | `config/grafana/provisioning/alerting/rules/dodo-alerts.yml` |
| CREATE | `config/grafana/provisioning/alerting/rules/common-alerts.yml` |
| MODIFY | `config/prometheus/alerts.yml` (remove synthetic test rule) |

## Do NOT

- Do not modify `docker-compose.yml`
- Do not modify `install.sh`
- Do not create any new services or containers
- Do not add any Python code
- Do not modify files outside the paths listed above
- Do not use `${VAR}` syntax in provisioning YAML — use `$VAR` (Grafana's
  interpolation syntax, no curly braces)
