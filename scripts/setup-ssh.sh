#!/usr/bin/env bash
# =============================================================================
# setup-ssh.sh — SSH hardening with fail2ban (Arch/Manjaro)
# =============================================================================
# Sets up OpenSSH + fail2ban for secure remote access.
#
# What this does:
#   - Installs openssh and fail2ban (pacman)
#   - Hardens sshd_config (disables root login, limits auth attempts)
#   - Password auth is LEFT ENABLED for now — switch to key-only after
#     your SSH key is confirmed working (see TODO at end of script output)
#   - Configures fail2ban sshd jail (ban after 5 failures, 1h ban)
#   - Enables and starts sshd + fail2ban
#   - Opens port 22 in ufw (if ufw is active)
#
# TODO (do this after confirming key-based auth works):
#   - Set PasswordAuthentication no in /etc/ssh/sshd_config
#   - Set ChallengeResponseAuthentication no
#   - Restrict SSH access to specific IPs via ufw or AllowUsers
#
# Run once:
#   sudo ./scripts/setup-ssh.sh
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

# =============================================================================
# 1. Install packages
# =============================================================================
echo -e "\n${BOLD}── Installing packages ──${NC}"

pacman -S --needed --noconfirm openssh fail2ban
ok "openssh and fail2ban installed."

# =============================================================================
# 2. Harden sshd_config
# =============================================================================
echo -e "\n${BOLD}── Hardening sshd_config ──${NC}"

SSHD_CONF="/etc/ssh/sshd_config"
SSHD_BACKUP="${SSHD_CONF}.bak.$(date +%Y%m%d%H%M%S)"

cp "$SSHD_CONF" "$SSHD_BACKUP"
info "Backed up sshd_config → $SSHD_BACKUP"

# Helper: set or add a directive in sshd_config
set_sshd() {
  local key="$1"
  local val="$2"
  if grep -qE "^#?${key}\s" "$SSHD_CONF"; then
    sed -i "s|^#\?${key}\s.*|${key} ${val}|" "$SSHD_CONF"
  else
    echo "${key} ${val}" >> "$SSHD_CONF"
  fi
}

set_sshd "Port"                       "22"
set_sshd "PermitRootLogin"            "no"
set_sshd "MaxAuthTries"               "3"
set_sshd "LoginGraceTime"             "30"
set_sshd "X11Forwarding"              "no"
set_sshd "AllowAgentForwarding"       "no"
set_sshd "AllowTcpForwarding"         "no"
set_sshd "PermitEmptyPasswords"       "no"
set_sshd "PrintLastLog"               "yes"

# Password auth ON for now — switch to 'no' after key is confirmed
set_sshd "PasswordAuthentication"     "yes"
set_sshd "PubkeyAuthentication"       "yes"

ok "sshd_config hardened (password auth still enabled — see TODO below)."

# Validate config before reloading
sshd -t || die "sshd_config validation failed — check $SSHD_CONF"
ok "sshd_config syntax valid."

# =============================================================================
# 3. Configure fail2ban
# =============================================================================
echo -e "\n${BOLD}── Configuring fail2ban ──${NC}"

cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
# Ban for 1 hour after failures
bantime  = 3600
# Look back 10 minutes
findtime = 600
# Ban after 5 failures
maxretry = 5
# Use systemd backend on Arch (journald)
backend  = systemd

[sshd]
enabled  = true
port     = ssh
logpath  = %(sshd_log)s
maxretry = 5
EOF

ok "fail2ban jail.local written (SSH: ban after 5 failures, 1h ban)."

# =============================================================================
# 4. Enable and start services
# =============================================================================
echo -e "\n${BOLD}── Enabling services ──${NC}"

for svc in sshd fail2ban; do
  systemctl enable --now "${svc}.service"
  ok "${svc}.service enabled and started."
done

# Reload sshd with new config (already running after enable --now)
systemctl reload sshd.service 2>/dev/null || systemctl restart sshd.service
ok "sshd reloaded with new config."

# =============================================================================
# 5. Firewall — open port 22
# =============================================================================
echo -e "\n${BOLD}── Firewall ──${NC}"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp >/dev/null 2>&1 && ok "ufw: port 22/tcp allowed." \
    || warn "ufw allow 22 may have already existed — skipping."
else
  warn "ufw not active — ensure port 22 is open in your router/firewall."
fi

# =============================================================================
# 6. Summary
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║     SSH hardening complete                       ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo -e "  ${BOLD}Connect via:${NC}  ssh $(whoami || echo 'your-user')@${HOST_IP}"
echo ""
echo -e "  ${BOLD}fail2ban status:${NC}  sudo fail2ban-client status sshd"
echo -e "  ${BOLD}Banned IPs:${NC}       sudo fail2ban-client status sshd | grep 'Banned IP'"
echo ""
echo -e "${YELLOW}${BOLD}┌─ TODO — switch to key-only auth ───────────────────────────────┐${NC}"
echo -e "${YELLOW}  1. Copy your public key to this machine:${NC}"
echo -e "       ssh-copy-id $(whoami || echo 'your-user')@${HOST_IP}"
echo -e "${YELLOW}  2. Confirm you can log in with the key (open a new terminal first).${NC}"
echo -e "${YELLOW}  3. Then disable password auth:${NC}"
echo -e "       sudo sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config"
echo -e "       sudo systemctl reload sshd"
echo -e "${YELLOW}  4. Consider restricting SSH to specific IPs via ufw:${NC}"
echo -e "       sudo ufw delete allow 22/tcp"
echo -e "       sudo ufw allow from <YOUR_IP> to any port 22 proto tcp"
echo -e "${YELLOW}${BOLD}└────────────────────────────────────────────────────────────────┘${NC}"
echo ""
