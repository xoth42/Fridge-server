#!/usr/bin/env bash
# =============================================================================
# bootstrap-vps.sh — install all prerequisites for fridge-server on a fresh
#                    Ubuntu VPS (tested on Ubuntu 24.04 / noble, kernel 6.8).
# =============================================================================
# What this does (idempotent — safe to re-run):
#   1. apt update + base packages (jq, gettext-base, ufw, rsync, sqlite3,
#      curl, ca-certificates, age, cron, util-linux for flock).
#   2. Docker Engine + compose plugin from Docker's official apt repo
#      (NOT docker.io — that ships an older engine and no compose plugin).
#   3. Enable docker.service.
#   4. ufw baseline: default deny incoming, allow OpenSSH. Leaves the
#      fridge-specific 8443 / 9091 rules to install.sh.
#   5. Pre-create /opt/fridge-server and (if BACKUP_DEST is local-form)
#      /var/backups/fridge with sane modes.
#   6. Prime SSH known_hosts for github.com so `git clone` over ssh works
#      non-interactively.
#
# What this does NOT do (deliberately — orthogonal to prereqs):
#   - clone the repo (you choose where it lives + the auth method)
#   - install nightly-backup.sh / fridge-backup.{service,timer}
#     (those are done after the repo is in place — see planning/backup-and-rsync.md)
#   - configure DuckDNS / DNS cutover (see planning/vps-migration.md)
#   - create a non-root user, harden SSH, configure unattended-upgrades
#     (host policy, not stack policy — separate concern)
#
# Usage (as root, on the new VPS):
#   curl -fsSL https://raw.githubusercontent.com/xoth42/Fridge-server/main/scripts/bootstrap-vps.sh \
#     | bash
#   # or, if the repo is already cloned:
#   ./scripts/bootstrap-vps.sh
# =============================================================================
set -Eeuo pipefail

# ─── helpers ─────────────────────────────────────────────────────────────────
C_RED='\033[0;31m'; C_GRN='\033[0;32m'; C_YEL='\033[1;33m'
C_BLU='\033[0;34m'; C_BLD='\033[1m';    C_NC='\033[0m'
info() { echo -e "${C_BLU}[INFO]${C_NC}  $*"; }
ok()   { echo -e "${C_GRN}[ OK ]${C_NC}  $*"; }
warn() { echo -e "${C_YEL}[WARN]${C_NC}  $*"; }
die()  { echo -e "${C_RED}[FAIL]${C_NC}  $*" >&2; exit 1; }
step() { echo -e "\n${C_BLD}── $* ──${C_NC}"; }

# ─── preflight ───────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "run as root (or via sudo): this script edits apt repos, ufw, and /opt"

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  ID_LOWER="${ID,,}"
else
  die "/etc/os-release missing — cannot detect distro"
fi

case "$ID_LOWER" in
  ubuntu|debian) : ;;
  *) die "this bootstrap targets Ubuntu/Debian (apt). Detected: $ID_LOWER. Adapt for $ID_LOWER manually." ;;
esac
info "Detected $PRETTY_NAME on $(uname -m), kernel $(uname -r)"

# ─── 1. base packages ────────────────────────────────────────────────────────
step "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# gettext-base ships envsubst without the full gettext toolchain.
# util-linux ships flock (almost always already installed; keep explicit).
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg lsb-release \
  jq gettext-base ufw rsync sqlite3 age cron util-linux
ok "base packages installed"

# ─── 2. Docker Engine + compose plugin ───────────────────────────────────────
step "Docker Engine + compose plugin"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "docker + compose plugin already present: $(docker --version | awk '{print $3}' | tr -d ,)"
else
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -s /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL "https://download.docker.com/linux/${ID_LOWER}/gpg" \
      | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
  fi
  CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}")"
  [[ -n "$CODENAME" ]] || die "could not resolve apt codename"
  cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID_LOWER} ${CODENAME} stable
EOF
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  ok "Docker installed from official apt repo"
fi

systemctl enable --now docker.service >/dev/null
ok "docker.service enabled and running"

# ─── 3. ufw baseline (do not lock ourselves out) ─────────────────────────────
step "ufw baseline"
# Order matters — allow SSH BEFORE enabling. The fridge-specific rules for
# 8443/tcp (Caddy) and 9091/tcp (Pushgateway, CIDR-restricted) are added by
# install.sh later, once .env is filled in.
ufw allow OpenSSH >/dev/null
ufw --force default deny incoming  >/dev/null
ufw --force default allow outgoing >/dev/null
if ufw status | grep -qi '^Status: active'; then
  ok "ufw already active"
else
  ufw --force enable >/dev/null
  ok "ufw enabled (default deny incoming, OpenSSH allowed)"
fi
ufw status verbose | sed 's/^/    /'

# ─── 4. directories ──────────────────────────────────────────────────────────
step "Directories"
install -d -m 0755 -o root -g root /opt/fridge-server
# /var/backups/fridge is only needed if BACKUP_DEST is a local path. Creating
# it costs nothing and avoids a "no such file" failure on the first nightly
# run if the user picks the local form. Production should use remote rsync.
install -d -m 0700 -o root -g root /var/backups/fridge
ok "/opt/fridge-server and /var/backups/fridge ready"

# ─── 5. known_hosts for github.com ───────────────────────────────────────────
step "SSH known_hosts"
install -d -m 0700 -o root -g root /root/.ssh
KNOWN=/root/.ssh/known_hosts
touch "$KNOWN" && chmod 600 "$KNOWN"
if ! ssh-keygen -F github.com -f "$KNOWN" >/dev/null 2>&1; then
  ssh-keyscan -H github.com 2>/dev/null >> "$KNOWN"
  ok "primed github.com host key"
else
  ok "github.com already in known_hosts"
fi

# ─── 6. report ───────────────────────────────────────────────────────────────
step "Versions"
printf '    docker         : %s\n'  "$(docker --version)"
printf '    compose plugin : %s\n'  "$(docker compose version --short 2>/dev/null || docker compose version | head -1)"
printf '    rsync          : %s\n'  "$(rsync --version | head -1)"
printf '    sqlite3        : %s\n'  "$(sqlite3 --version | awk '{print $1}')"
printf '    jq             : %s\n'  "$(jq --version)"
printf '    age            : %s\n'  "$(age --version 2>&1)"
printf '    ufw            : %s\n'  "$(ufw --version | head -1)"

cat <<'EOF'

── Next steps ─────────────────────────────────────────────────────────────────
  1. Place the repo at /opt/fridge-server (clone, or restore from a migration
     snapshot per planning/vps-migration.md).
  2. Fill /opt/fridge-server/.env from .env.example. In particular for this
     host:
       - DOMAIN / GRAFANA_PUBLIC_URL
       - DuckDNS token (flip DNS to this host's public IP before LE renewal)
       - ALLOWED_PUSH_CIDR  (until set, install.sh leaves 9091 wide open)
       - BACKUP_DEST        (external rsync target — see planning doc)
       - BACKUP_SSH_KEY     (if using a dedicated identity)
  3. Run ./install.sh from the repo root.
  4. Install nightly-backup.sh + the systemd unit + timer:
       sudo install -m 0755 nightly-backup.sh                       /usr/local/sbin/
       sudo install -m 0644 planning/fridge-backup.service.example  /etc/systemd/system/fridge-backup.service
       sudo install -m 0644 planning/fridge-backup.timer.example    /etc/systemd/system/fridge-backup.timer
       sudo systemctl daemon-reload
       sudo systemctl enable --now fridge-backup.timer
     (Unit files are embedded in planning/backup-and-rsync.md — copy them
      out the first time, then commit if you want them version-controlled.)

EOF
ok "bootstrap complete"
