# Viewing Fridge Data — Command Reference

All services bind to `127.0.0.1` (localhost-only) except Pushgateway.
Run these commands on the server, or open an SSH tunnel first.

```
ssh -L 9090:127.0.0.1:9090 -L 9091:127.0.0.1:9091 -L 3000:127.0.0.1:3000 <user>@<server-ip>
```

---

## Service Endpoints (quick reference)

| Service       | URL                          | Notes                              |
|---------------|------------------------------|------------------------------------|
| Prometheus    | http://localhost:9090        | localhost-only, no auth            |
| Pushgateway   | http://localhost:9091        | public port; guarded by ufw        |
| Grafana       | http://localhost:3000        | localhost HTTP; HTTPS via Caddy    |
| Alertmanager  | http://localhost:9093        | localhost-only                     |
| Grafana (TLS) | https://fridge.zickers.us:8443    | production only                    |

---

## Prometheus API — Instant Queries

Append `| jq` for readable JSON output (`sudo apt install jq` / `pacman -S jq`, also it is installed on server machine).

### Enumerate everything

```bash
# List all metric names currently in Prometheus
curl -sG 'http://localhost:9090/api/v1/label/__name__/values' | jq '.data[]'

# List all known fridge instance names (job=sensor_data)
curl -sG 'http://localhost:9090/api/v1/label/instance/values' | jq '.data[]'

# View every raw metric currently pushed for a single fridge
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-manny"}' | jq '.data.result[]'
```

### Data freshness — is the fridge still pushing?

```bash
# Seconds since last push (check both fridges)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=time() - last_push_timestamp_seconds{job="sensor_data"}' \
  | jq '.data.result[] | {instance: .metric.instance, staleness_s: .value[1]}'

# Shorthand: show last_push_timestamp as human time (needs jq date)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=last_push_timestamp_seconds{job="sensor_data"}' \
  | jq '.data.result[] | {instance: .metric.instance, last_push: (.value[1] | tonumber | todate)}'
```

### Manny — current snapshot

```bash
# All temperatures at once (50K / 4K / Still / MXC / CP flanges)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-manny", job="sensor_data", __name__=~"ch._t_kelvin"}' \
  | jq '.data.result[] | {metric: .metric.__name__, value: .value[1]}'

# Mixing chamber temperature (≈15 mK when cold)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=ch6_t_kelvin{instance="fridge-manny"}' \
  | jq '.data.result[0].value[1]'

# CP flange temperature (CH9 — present on Manny, absent on Dodo)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=ch9_t_kelvin{instance="fridge-manny"}' \
  | jq '.data.result[0].value[1]'

# Mixture flow rate (mmol/s)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=flowmeter_mmol_per_s{instance="fridge-manny"}' \
  | jq '.data.result[0].value[1]'

# Maxigauge pressures (all 6 channels)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-manny", __name__=~"maxigauge_ch._pressure_mbar"}' \
  | jq '.data.result[] | {metric: .metric.__name__, mbar: .value[1]}'

# Heater power
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-manny", __name__=~"heater_._watts"}' \
  | jq '.data.result[] | {metric: .metric.__name__, watts: .value[1]}'

# Compressor pressures (high/low pressure & water temps)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-manny", __name__=~"cpa.*"}' \
  | jq '.data.result[] | {metric: .metric.__name__, value: .value[1]}'
```

### Dodo — current snapshot

> **Note:** Dodo has no uploader written yet (as of 2026-04). These commands
> will return empty results until `push_metrics.py` is deployed on Dodo's PC.
> Dodo instance label will be `fridge-dodo`.

```bash
# All temperatures (CH1/2/5/6 confirmed present; CH9 absent on Dodo)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-dodo", __name__=~"ch._t_kelvin"}' \
  | jq '.data.result[] | {metric: .metric.__name__, value: .value[1]}'

# MXC temperature
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=ch6_t_kelvin{instance="fridge-dodo"}' \
  | jq '.data.result[0].value[1]'

# Pressure channels (CH1/2/5/6 pressure readings — confirmed on Dodo)
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-dodo", __name__=~"ch._p_mbar"}' \
  | jq '.data.result[] | {metric: .metric.__name__, mbar: .value[1]}'

# Flow rate
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=flowmeter_mmol_per_s{instance="fridge-dodo"}' \
  | jq '.data.result[0].value[1]'
```

### Sid — current snapshot

> **Note:** Sid's channel layout is unknown (sid.config has empty channel
> lists). Run `diagnose.py` on Sid's machine to confirm which CH files exist.

```bash
# All metrics Sid is currently pushing
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={instance="fridge-sid", job="sensor_data"}' \
  | jq '.data.result[] | {metric: .metric.__name__, value: .value[1]}'

# Staleness check
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=time() - last_push_timestamp_seconds{instance="fridge-sid"}' \
  | jq '.data.result[0].value[1]'
```

---

## Prometheus API — Range Queries (time series)

```bash
# MXC temperature over the last 6 hours (1-minute resolution) — Manny
curl -sG 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=ch6_t_kelvin{instance="fridge-manny"}' \
  --data-urlencode "start=$(date -d '6 hours ago' +%s)" \
  --data-urlencode "end=$(date +%s)" \
  --data-urlencode 'step=60' \
  | jq '.data.result[0].values[] | [.[0] | todate, .[1]]'

# Flow rate over the last 24 hours — Manny
curl -sG 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=flowmeter_mmol_per_s{instance="fridge-manny"}' \
  --data-urlencode "start=$(date -d '24 hours ago' +%s)" \
  --data-urlencode "end=$(date +%s)" \
  --data-urlencode 'step=300' \
  | jq '.data.result[0].values[-5:] | .[] | [.[0] | todate, .[1]]'

# How many minutes since last push (1-hour window, 1-min step)
curl -sG 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=(time() - last_push_timestamp_seconds{job="sensor_data"}) / 60' \
  --data-urlencode "start=$(date -d '1 hour ago' +%s)" \
  --data-urlencode "end=$(date +%s)" \
  --data-urlencode 'step=60' \
  | jq '.data.result[] | {instance: .metric.instance, values: .values[-3:]}'
```

---

## Pushgateway — Raw Metrics

The Pushgateway `/metrics` endpoint returns Prometheus text format. This shows
exactly what each fridge computer last pushed, before Prometheus scrapes it.

```bash
# ALL currently pushed metrics (text format — can be long)
curl -s http://localhost:9091/metrics

# Filter to one fridge only
curl -s http://localhost:9091/metrics | grep 'instance="fridge-manny"'

# Show only temperature metrics for Manny
curl -s http://localhost:9091/metrics | grep 'instance="fridge-manny"' | grep '_t_kelvin'

# Show only pressure metrics (maxigauge) for Manny
curl -s http://localhost:9091/metrics | grep 'instance="fridge-manny"' | grep 'maxigauge'

# Check how many distinct metric names are being pushed per fridge
curl -s http://localhost:9091/metrics \
  | grep 'instance="fridge-manny"' \
  | grep -v '^#' \
  | awk '{print $1}' | sed 's/{.*//' | sort -u
```

---

## Alertmanager

```bash
# Active alerts right now
curl -s http://localhost:9093/api/v2/alerts | jq '.[] | {name: .labels.alertname, instance: .labels.instance, state: .status.state}'

# Is Alertmanager healthy?
curl -s http://localhost:9093/-/healthy
```

---

## Grafana API

Grafana HTTP API requires admin credentials. Replace `admin:PASSWORD` with
the values set in `.env` (`GF_ADMIN_USER` / `GF_ADMIN_PASSWORD`).

```bash
# Health check (no auth needed)
curl -s http://localhost:3000/api/health | jq

# List provisioned datasources
curl -s http://admin:PASSWORD@localhost:3000/api/datasources | jq '.[].name'

# List all dashboards
curl -s http://admin:PASSWORD@localhost:3000/api/search?type=dash-db | jq '.[] | {title, url}'

# Get Prometheus datasource UID (needed for ad-hoc panel API calls)
curl -s http://admin:PASSWORD@localhost:3000/api/datasources/name/Prometheus | jq '{uid, url}'

# Query a metric through the Grafana proxy (useful for confirming the datasource works)
curl -sG http://admin:PASSWORD@localhost:3000/api/datasources/proxy/uid/P1809F7CD0C75ACF3/api/v1/query \
  --data-urlencode 'query=ch6_t_kelvin{instance="fridge-manny"}' \
  | jq '.data.result[0].value[1]'
```

---

## Pushgateway — Push Test Data (CI / debugging)

```bash
# Push synthetic test metrics for both fridge-manny and fridge-sid
cd /path/to/Fridge-server
python3 testdata/pushtestmetrics.py --pushgateway-url http://localhost:9091

# Verify Prometheus received them
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query={job="sensor_data", __name__=~"ch._t_kelvin"}' \
  | jq '.data.result[] | {instance: .metric.instance, metric: .metric.__name__, val: .value[1]}'
```

---

## PromQL Quick Reference

| Goal | Expression |
|------|------------|
| Latest value — all fridges | `ch6_t_kelvin{job="sensor_data"}` |
| Latest value — one fridge | `ch6_t_kelvin{instance="fridge-manny"}` |
| All temps matching regex | `{__name__=~"ch._t_kelvin", instance="fridge-manny"}` |
| Seconds since last push | `time() - last_push_timestamp_seconds{job="sensor_data"}` |
| Max temp over 1h window | `max_over_time(ch6_t_kelvin{instance="fridge-manny"}[1h])` |
| Rate of change (/min) | `rate(ch6_t_kelvin{instance="fridge-manny"}[5m]) * 60` |
| All metrics for fridge | `{instance="fridge-manny", job="sensor_data"}` |

---

## Notes on Current State (as of 2026-04)

- **Manny**: Bluefors, CH1/2/5/6/9. Currently pointed at a dead EC2 link —
  redirect `server.env` `PUSHGATEWAY_URL` to new server IP once deployed.
- **Sid**: Bluefors, online and pushing. Channel layout unconfirmed — run
  `diagnose.py` on Sid's machine and update `fridge_configs/sid.config`.
- **Dodo**: Oxford Instruments. No uploader written yet; queries will return
  empty. Instance label will be `fridge-dodo` when deployed.
