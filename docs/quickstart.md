# Quick Start

```bash
cp .env.example .env
$EDITOR .env
./install.sh
```

At minimum, set a real `GF_ADMIN_PASSWORD`. For production, also configure the domain, public URL, SMTP credentials, Slack webhook/signing secret, DuckDNS, name.com API credentials, and `ALLOWED_PUSH_CIDR`.

To run the intrusive alert UI end-to-end test during install:

```bash
RUN_E2E=true ./install.sh
```

## URLs

- Grafana: `http://localhost:3000`
- Alert UI through Caddy: `https://<DOMAIN>/alerts/`
- Prometheus: `http://localhost:9090`
- Alertmanager: `http://localhost:9093`
- Pushgateway: `http://<server-ip>:9091`