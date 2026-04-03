#!/usr/bin/env bash
# =============================================================================
# setup-remote-desktop.sh — Sunshine game streaming / remote desktop (Arch/Manjaro)
# =============================================================================
# Installs and configures Sunshine (https://app.lizardbyte.dev/Sunshine/)
# for remote desktop access via the Moonlight client.
#
# Sunshine is free, open-source, and works over WAN via direct connection
# or relay. It uses hardware GPU encoding (NVIDIA/AMD/Intel) for low latency.
#
# Client app (Moonlight) is free on all platforms:
#   https://moonlight-stream.org
#
# What this script does:
#   - Installs Sunshine via Flatpak (most reliable cross-DE method on Arch)
#   - Grants Sunshine the KMS/DRM capture capability (for Wayland/GNOME)
#   - Opens Sunshine's required ports in ufw
#   - Enables Sunshine to autostart on login (systemd user service)
#   - Prints pairing instructions
#
# Wayland note (GNOME):
#   Sunshine captures via KMS/DRM on Wayland. This requires a small capability
#   grant. The script handles this automatically.
#   On i3 (X11), Sunshine uses standard X11 capture — no extra setup needed.
#
# Ports used by Sunshine (open these on your router for WAN access):
#   TCP: 47984, 47989, 48010
#   UDP: 47998, 47999, 48000, 48002, 48010
#
# Run once:
#   sudo ./scripts/setup-remote-desktop.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# ── Arch/Manjaro guard ────────────────────────────────────────────────────────
[[ -f /etc/arch-release ]] \
  || die "This script is for Arch-based systems only (Arch, Manjaro, EndeavourOS, etc.)."

[[ "$EUID" -eq 0 ]] \
  || die "Run this script with sudo: sudo $0"

# We need the real user for user-scope commands
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo '')}"
[[ -n "$REAL_USER" ]] \
  || die "Could not determine the calling user. Run via sudo, not as root directly."

REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

# =============================================================================
# 1. Install Flatpak (if not present)
# =============================================================================
echo -e "\n${BOLD}── Flatpak ──${NC}"

if ! command -v flatpak >/dev/null 2>&1; then
  info "Installing flatpak..."
  pacman -S --needed --noconfirm flatpak
  ok "flatpak installed."
else
  ok "flatpak already installed."
fi

# Add Flathub if not present
if ! flatpak remote-list --system | grep -q flathub; then
  info "Adding Flathub remote..."
  flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  ok "Flathub added."
else
  ok "Flathub already configured."
fi

# =============================================================================
# 2. Install Sunshine
# =============================================================================
echo -e "\n${BOLD}── Installing Sunshine ──${NC}"

if flatpak list --system | grep -q "dev.lizardbyte.app.Sunshine"; then
  info "Sunshine already installed — updating..."
  flatpak update --system --noninteractive dev.lizardbyte.app.Sunshine
else
  info "Installing Sunshine from Flathub..."
  flatpak install --system --noninteractive flathub dev.lizardbyte.app.Sunshine
fi
ok "Sunshine installed."

# =============================================================================
# 3. KMS/DRM capture capability (needed for Wayland / GNOME)
# =============================================================================
echo -e "\n${BOLD}── KMS capture capability ──${NC}"

# Sunshine needs to read /dev/dri/* for KMS capture on Wayland.
# Grant it via the Flatpak override.
flatpak override --system \
  --device=dri \
  dev.lizardbyte.app.Sunshine
ok "DRI device access granted to Sunshine (Wayland KMS capture)."

# =============================================================================
# 4. Autostart via systemd user service
# =============================================================================
echo -e "\n${BOLD}── Autostart ──${NC}"

SYSTEMD_USER_DIR="${REAL_HOME}/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

cat > "${SYSTEMD_USER_DIR}/sunshine.service" << 'EOF'
[Unit]
Description=Sunshine game streaming server
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/flatpak run dev.lizardbyte.app.Sunshine
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF

chown "${REAL_USER}:${REAL_USER}" "${SYSTEMD_USER_DIR}/sunshine.service"

# Enable via loginctl (user lingering ensures it runs without login)
loginctl enable-linger "$REAL_USER"

# Enable the service as the real user
sudo -u "$REAL_USER" \
  XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
  systemctl --user daemon-reload

sudo -u "$REAL_USER" \
  XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
  systemctl --user enable sunshine.service

ok "Sunshine autostart enabled (systemd user service, linger on)."

# Start it now
sudo -u "$REAL_USER" \
  XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
  systemctl --user start sunshine.service 2>/dev/null \
  && ok "Sunshine started." \
  || warn "Could not start Sunshine now (may need a graphical session). It will start on next login."

# =============================================================================
# 5. Firewall — open Sunshine ports
# =============================================================================
echo -e "\n${BOLD}── Firewall ──${NC}"

open_port() {
  local port="$1" proto="$2"
  ufw allow "${port}/${proto}" >/dev/null 2>&1 \
    && ok "ufw: ${port}/${proto} allowed." \
    || warn "ufw: ${port}/${proto} rule may already exist."
}

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  # TCP ports
  for port in 47984 47989 48010; do
    open_port "$port" tcp
  done
  # UDP ports
  for port in 47998 47999 48000 48002 48010; do
    open_port "$port" udp
  done
else
  warn "ufw not active — open these ports on your router/firewall manually:"
  warn "  TCP: 47984, 47989, 48010"
  warn "  UDP: 47998, 47999, 48000, 48002, 48010"
fi

# =============================================================================
# 6. Summary and pairing instructions
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     Sunshine remote desktop ready                    ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo -e "  ${BOLD}Sunshine web UI:${NC}  https://localhost:47990"
echo -e "  ${BOLD}(on this machine — set a username/password on first launch)${NC}"
echo ""
echo -e "${BOLD}  How to connect from Moonlight:${NC}"
echo -e "  1. Install Moonlight on your client: https://moonlight-stream.org"
echo -e "  2. Click 'Add PC' and enter this machine's IP or hostname:"
echo -e "       Local: ${HOST_IP}"
echo -e "       WAN:   <your public IP or fridge.zickers.us>"
echo -e "  3. Enter the PIN shown in Moonlight into Sunshine's web UI:"
echo -e "       https://localhost:47990  →  PIN tab"
echo ""
echo -e "${BOLD}  Router port forwarding required for WAN access:${NC}"
printf "    %-12s  %s\n" "TCP 47984"  "Sunshine HTTPS"
printf "    %-12s  %s\n" "TCP 47989"  "Sunshine HTTP"
printf "    %-12s  %s\n" "TCP 48010"  "Control stream"
printf "    %-12s  %s\n" "UDP 47998"  "Video stream"
printf "    %-12s  %s\n" "UDP 47999"  "Control"
printf "    %-12s  %s\n" "UDP 48000"  "Audio stream"
printf "    %-12s  %s\n" "UDP 48002"  "Video stream (alt)"
echo ""
echo -e "${YELLOW}${BOLD}┌─ GNOME/Wayland note ───────────────────────────────────────────┐${NC}"
echo -e "${YELLOW}  Sunshine uses KMS/DRM capture on Wayland. If the stream is black,${NC}"
echo -e "${YELLOW}  open Sunshine's web UI → Config → Set 'Capture method' to 'KMS'.${NC}"
echo -e "${YELLOW}  On i3 (X11), standard X11 capture works without extra config.${NC}"
echo -e "${YELLOW}${BOLD}└────────────────────────────────────────────────────────────────┘${NC}"
echo ""
echo -e "  ${BOLD}Check status:${NC}  sudo -u ${REAL_USER} systemctl --user status sunshine"
echo ""
