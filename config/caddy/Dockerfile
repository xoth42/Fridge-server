# Custom Caddy image with the DuckDNS DNS provider plugin.
# Required for DNS-01 TLS certificate challenge (no inbound port 80/443 needed).
# Built automatically by docker compose when using: build: ./config/caddy

FROM caddy:2.8.4-builder AS builder
RUN xcaddy build \
    --with github.com/caddy-dns/namedotcom

FROM caddy:2.8.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
