# Fridge Monitor Server

Monitoring and alerting stack for Wang Lab dilution refrigerators. Fridge computers push sensor metrics to Pushgateway; Prometheus stores them; Grafana shows dashboards and evaluates alert rules; alerts can be delivered by email and Slack.

## Current fridges

- Manny (`fridge-manny`)
- Dodo (`fridge-dodo`)

Sid/Oxford support is not wired into the live metric config yet.

## Screenshots

### Alert UI

![Alert UI snapshot](alertui.png)

### Grafana Dashboard

![Grafana dashboard snapshot](cooldowntmp.png)

### Slack Integration

![Slack command snapshot](slackcmd.png)