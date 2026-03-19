# Fridge Monitor — Server Stack

Monitoring stack for Wang Lab dilution refrigerators. Receives temperature and
pressure metrics from fridge computers, stores them in Prometheus, and displays
them in Grafana. Sends alerts via Slack and email.

**Fridges:** Manny (Bluefors), Sid (Bluefors), Dodo (Oxford Instruments — pending)

For detailed docs see [docs/](docs/).

---

## Quickstart

```bash
cp .env.example .env
nano .env          # set GF_ADMIN_PASSWORD at minimum; see comments for all options

./install.sh       # pulls images, starts stack, runs health checks, prints URLs
```

Grafana will be available at `http://localhost:3000` locally, or at
`https://fridge.zickers.us` in production.

**Fridge computers:** set `PUSHGATEWAY_URL=http://<server-ip>:9091` in each
fridge's `server.env`.

---

## Prerequisites

- Docker Engine with Compose plugin (`docker compose version` must work)
- `gettext` for `envsubst`: `apt install gettext` / `pacman -S gettext`
- Ports 80, 443, and 9091 reachable from outside (see Port Forwarding below)

---

## Port Forwarding

The stack requires three ports to be reachable from outside your router.
Configure these as port forwarding rules on your router pointing at the server's
local IP address.

| Port | Protocol | Service | Who connects |
|------|----------|---------|--------------|
| 8443 | TCP | Caddy (Grafana HTTPS) | Public internet, lab members |
| 9091 | TCP | Pushgateway (metric ingestion) | Fridge computers on college network |

### How to set this up

1. Find your server's **local IP**: `hostname -I | awk '{print $1}'`
   — this is typically something like `192.168.1.x`. Assign it a static local IP
   in your router's DHCP settings (usually called "DHCP reservation" or "static
   lease") so it does not change.

2. In your router admin panel (usually at `192.168.1.1` or `192.168.0.1`),
   find **Port Forwarding** (sometimes under "NAT", "Virtual Server", or
   "Applications & Gaming").

3. Add three rules:
   - External port 8443 → server local IP, internal port 8443, TCP
   - External port 9091 → server local IP, internal port 9091, TCP

4. Verify from outside the network (e.g. phone on mobile data):
   ```bash
   curl -I https://fridge.zickers.us        # should return 200
   curl http://<your-public-ip>:9091/-/healthy  # should return OK
   ```

### Restricting Pushgateway access (recommended)

Port 9091 only needs to be reachable from the college network where the fridge
computers live. Once IT provides the college network CIDR, set it in `.env`:

```bash
ALLOWED_PUSH_CIDR=10.x.0.0/16    # fill in your college's range
```

Then re-run `./install.sh` — it will add a `ufw` firewall rule blocking all
other IPs from port 9091. Port 80 and 443 remain open to all.

---

## Managing the stack

```bash
# Stop
docker compose --profile production down

# Restart a single service
docker compose restart grafana

# View logs
docker compose logs -f grafana
docker compose logs -f duckdns

# Update config and apply (safe to re-run)
nano .env
./install.sh
```

---

## CI

GitHub Actions runs on every push: starts the stack, pushes test metrics,
verifies Prometheus and Grafana, then tears down. See
[.github/workflows/ci.yml](.github/workflows/ci.yml).
