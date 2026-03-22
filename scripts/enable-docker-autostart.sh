#!/usr/bin/env bash
# =============================================================================
# enable-docker-autostart.sh — Enable Docker to start on boot (Arch/Manjaro)
# =============================================================================
# Enables docker.service and containerd.service via systemd so the Docker
# daemon and the fridge monitoring stack restart automatically after a reboot.
#
# Run once after initial Docker installation:
#   sudo ./scripts/enable-docker-autostart.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
die()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# ── Arch/Manjaro guard ────────────────────────────────────────────────────────
[[ -f /etc/arch-release ]] \
  || die "This script is for Arch-based systems only (Arch, Manjaro, EndeavourOS, etc.)."

# ── Root check ────────────────────────────────────────────────────────────────
[[ "$EUID" -eq 0 ]] \
  || die "Run this script with sudo: sudo $0"

echo -e "\n${BOLD}── Docker autostart setup ──${NC}"

# ── Enable services ───────────────────────────────────────────────────────────
for svc in containerd docker; do
  if systemctl is-enabled --quiet "${svc}.service" 2>/dev/null; then
    ok "${svc}.service already enabled — skipping."
  else
    info "Enabling ${svc}.service..."
    systemctl enable "${svc}.service"
    ok "${svc}.service enabled."
  fi

  if ! systemctl is-active --quiet "${svc}.service" 2>/dev/null; then
    info "Starting ${svc}.service..."
    systemctl start "${svc}.service"
  fi
done

# ── Also enable docker.socket (allows non-root socket activation) ─────────────
if systemctl is-enabled --quiet docker.socket 2>/dev/null; then
  ok "docker.socket already enabled."
else
  info "Enabling docker.socket..."
  systemctl enable docker.socket
  ok "docker.socket enabled."
fi

# ── Status summary ────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Service status:${NC}"
for svc in containerd docker docker.socket; do
  state=$(systemctl is-active "${svc}" 2>/dev/null || echo "inactive")
  enabled=$(systemctl is-enabled "${svc}" 2>/dev/null || echo "disabled")
  printf "  %-22s  active=%-10s  enabled=%s\n" "${svc}" "$state" "$enabled"
done

echo ""
ok "Docker will now start automatically on boot."
echo -e "  To verify after a reboot: ${BOLD}systemctl status docker${NC}"
echo ""
