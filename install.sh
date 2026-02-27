#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: Docker is not installed or not on PATH." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  echo "Error: Docker Compose is not available. Install docker compose plugin or docker-compose." >&2
  exit 1
fi

echo "Using compose command: $COMPOSE_CMD"
echo "Pulling container images for deterministic first run..."
$COMPOSE_CMD pull

echo
echo "Stack ready. Common commands:"
echo "  Start: $COMPOSE_CMD up -d"
echo "  Stop : $COMPOSE_CMD down"
echo "  Push test metrics: python3 testdata/pushtestmetrics.py --pushgateway-url http://localhost:9091"
