#!/usr/bin/env bash
# =============================================================================
# nightly-backup.sh — hot backup of the fridge-server compose stack.
# =============================================================================
# Discovers Docker named volumes from `docker compose config` and their host
# paths from `docker volume inspect`. Independent of Docker data-root location.
#
# Per-volume strategy:
#   prometheus-data   : POST /api/v1/admin/tsdb/snapshot, then rsync snapshot
#   grafana-data      : sqlite3 .backup grafana.db (online backup API), rsync
#   alertmanager-data : docker pause -> rsync -> docker unpause
#   *                 : plain rsync (idempotent / restart-tolerant data)
# Repo tree (compose dir): rsync, excluding caches and mounted data dirs.
#
# Rotation: hardlinked daily snapshots via rsync --link-dest. KEEP_DAILY ago
# snapshots are pruned. Extend in prune_local() for GFS.
#
# Usage:
#   ./nightly-backup.sh [COMPOSE_DIR] [DEST]
#   DEST = "/abs/path"  or  "user@host:/abs/path"
#
# Env overrides: COMPOSE_DIR, BACKUP_DEST, KEEP_DAILY, LOCK
# =============================================================================
set -Eeuo pipefail
shopt -s nullglob

COMPOSE_DIR="${1:-${COMPOSE_DIR:-/opt/fridge-server}}"
DEST="${2:-${BACKUP_DEST:-/var/backups/fridge}}"
KEEP_DAILY="${KEEP_DAILY:-7}"
LOCK="${LOCK:-/var/lock/fridge-backup.lock}"
LOG_TAG="fridge-backup"

log() { logger -t "$LOG_TAG" -- "$*" 2>/dev/null || true; printf '[%s] %s\n' "$(date +%FT%T)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }
trap 'die "interrupted on line $LINENO"' ERR

# ── single-instance lock ─────────────────────────────────────────────────────
exec 9>"$LOCK" || die "cannot open lock $LOCK"
flock -n 9    || die "another backup is running"

# ── preflight ────────────────────────────────────────────────────────────────
for bin in docker rsync jq curl flock; do
  command -v "$bin" >/dev/null || die "$bin not in PATH"
done
[[ -f "$COMPOSE_DIR/docker-compose.yml" ]] || die "no docker-compose.yml at $COMPOSE_DIR"
cd "$COMPOSE_DIR"

# ── discover compose project + named volumes ─────────────────────────────────
PROJECT="$(docker compose config --format json | jq -r '.name')"
[[ -n "$PROJECT" && "$PROJECT" != "null" ]] || die "compose project name not resolved"
log "compose project: $PROJECT  (dir: $COMPOSE_DIR)"

declare -A VOL_HOST
# Discover by introspecting each running compose container's actual Mounts.
# This is robust against project-name drift: we don't have to guess
# "<project>_<short>" — Docker tells us the real volume name and host path.
mapfile -t _cids < <(docker compose ps -q 2>/dev/null)
[[ ${#_cids[@]} -gt 0 ]] || die "no running containers for project $PROJECT (start the stack first)"

for cid in "${_cids[@]}"; do
  while IFS=$'\t' read -r vol_name dest host_path; do
    [[ -z "$vol_name" ]] && continue
    [[ -d "$host_path" ]] || { log "skip $vol_name: $host_path not a dir"; continue; }
    # Strip the project prefix (if any) for use as the dispatch key.
    short="${vol_name#${PROJECT}_}"
    short="${short#Fridge-server_}"            # legacy capitalisation fallback
    VOL_HOST["$short"]="$host_path"
    log "vol $short  ->  $host_path  (docker name: $vol_name, mounted at $dest)"
  done < <(
    docker inspect "$cid" --format '
{{- range .Mounts -}}
{{- if eq .Type "volume" }}{{ .Name }}{{"\t"}}{{ .Destination }}{{"\t"}}{{ .Source }}
{{ end -}}
{{- end -}}'
  )
done

[[ ${#VOL_HOST[@]} -gt 0 ]] || die "discovered zero named volumes; check 'docker volume ls'"

# ── staging ──────────────────────────────────────────────────────────────────
STAMP="$(date +%Y-%m-%d_%H%M%S)"
STAGE="$(mktemp -d -p "${TMPDIR:-/var/tmp}" fridge-backup.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/volumes" "$STAGE/repo"

RSYNC_OPTS=(-aHAX --numeric-ids --delete)

# ── per-volume handlers ──────────────────────────────────────────────────────
quiesce_prometheus() {
  local host_path="$1"
  log "prometheus: tsdb snapshot via admin API"
  # Prometheus is bound to 127.0.0.1:9090 in this stack; that's where we hit it.
  local snap_id
  snap_id="$(curl -fsS -X POST \
              http://127.0.0.1:9090/api/v1/admin/tsdb/snapshot \
              | jq -r '.data.name')" \
    || die "prometheus snapshot API failed (admin API enabled? container up?)"
  [[ -n "$snap_id" && "$snap_id" != "null" ]] || die "empty snapshot id"
  log "prometheus: snapshot id $snap_id"

  rsync "${RSYNC_OPTS[@]}" \
        "$host_path/snapshots/$snap_id/" \
        "$STAGE/volumes/prometheus-data/"

  # Reclaim space on the live volume. Snapshots are hardlinked, so cheap to drop.
  rm -rf "$host_path/snapshots/$snap_id" 2>/dev/null \
    || docker exec fridge-prometheus rm -rf "/prometheus/snapshots/$snap_id" 2>/dev/null \
    || log "warn: could not remove live snapshot $snap_id (manual cleanup later)"
}

quiesce_grafana() {
  local host_path="$1"
  log "grafana: sqlite3 .backup grafana.db (online backup API)"
  docker exec fridge-grafana sh -c '
      set -e
      rm -f /var/lib/grafana/grafana.db.bak
      sqlite3 /var/lib/grafana/grafana.db ".backup /var/lib/grafana/grafana.db.bak"
  ' || die "grafana sqlite3 .backup failed"

  rsync "${RSYNC_OPTS[@]}" \
        --exclude='grafana.db' \
        --exclude='grafana.db-wal' \
        --exclude='grafana.db-shm' \
        "$host_path/" "$STAGE/volumes/grafana-data/"
  # Promote the consistent copy to the canonical filename on the backup side.
  mv "$STAGE/volumes/grafana-data/grafana.db.bak" \
     "$STAGE/volumes/grafana-data/grafana.db"
  docker exec fridge-grafana rm -f /var/lib/grafana/grafana.db.bak || true
}

quiesce_alertmanager() {
  local host_path="$1"
  log "alertmanager: pause -> rsync -> unpause"
  docker pause fridge-alertmanager >/dev/null
  if ! rsync "${RSYNC_OPTS[@]}" \
        "$host_path/" "$STAGE/volumes/alertmanager-data/"; then
    docker unpause fridge-alertmanager >/dev/null || true
    die "alertmanager rsync failed"
  fi
  docker unpause fridge-alertmanager >/dev/null
}

quiesce_default() {
  local name="$1" host_path="$2"
  log "$name: plain rsync"
  rsync "${RSYNC_OPTS[@]}" "$host_path/" "$STAGE/volumes/$name/"
}

for vol in "${!VOL_HOST[@]}"; do
  mkdir -p "$STAGE/volumes/$vol"
  case "$vol" in
    prometheus-data)   quiesce_prometheus   "${VOL_HOST[$vol]}" ;;
    grafana-data)      quiesce_grafana      "${VOL_HOST[$vol]}" ;;
    alertmanager-data) quiesce_alertmanager "${VOL_HOST[$vol]}" ;;
    *)                 quiesce_default "$vol" "${VOL_HOST[$vol]}" ;;
  esac
done

# ── repo / compose dir (config + .env + scripts) ─────────────────────────────
log "repo: rsync compose dir"
rsync "${RSYNC_OPTS[@]}" \
      --exclude='.git/' \
      --exclude='__pycache__/' \
      --exclude='*.pyc' \
      --exclude='site/' \
      --exclude='node_modules/' \
      --exclude='prometheus-data/' \
      --exclude='grafana-data/' \
      --exclude='alertmanager-data/' \
      --exclude='caddy_data/' \
      --exclude='caddy_config/' \
      "$COMPOSE_DIR/" "$STAGE/repo/"

# ── manifest (for sanity on restore) ─────────────────────────────────────────
{
  echo "# fridge-server backup manifest"
  echo "timestamp:  $STAMP"
  echo "host:       $(hostname -f 2>/dev/null || hostname)"
  echo "compose:    $COMPOSE_DIR  (project: $PROJECT)"
  echo
  echo "## volumes"
  for vol in "${!VOL_HOST[@]}"; do
    printf '  %-22s %-60s %s\n' \
      "$vol" "${VOL_HOST[$vol]}" \
      "$(du -sh "${VOL_HOST[$vol]}" 2>/dev/null | cut -f1)"
  done
  echo
  echo "## docker compose ps"
  docker compose ps 2>/dev/null || true
  echo
  echo "## image digests (sha256, for reproducible restore)"
  docker compose images 2>/dev/null || true
} > "$STAGE/MANIFEST.txt"

# ── ship with hardlink rotation ──────────────────────────────────────────────
ship() {
  local stage="$1" dest="$2"
  if [[ "$dest" == *:* ]]; then
    # remote
    local host="${dest%%:*}" path="${dest#*:}"
    ssh "$host" "mkdir -p '$path/daily'"
    rsync "${RSYNC_OPTS[@]}" \
          --link-dest="$path/current/" \
          -e ssh \
          "$stage/" "$host:$path/daily/$STAMP/"
    ssh "$host" "ln -sfn 'daily/$STAMP' '$path/current'"
  else
    mkdir -p "$dest/daily"
    rsync "${RSYNC_OPTS[@]}" \
          --link-dest="$dest/current/" \
          "$stage/" "$dest/daily/$STAMP/"
    ln -sfn "daily/$STAMP" "$dest/current"
  fi
}
log "shipping to $DEST"
ship "$STAGE" "$DEST"

# ── prune ────────────────────────────────────────────────────────────────────
prune_local() {
  local root="$1" keep="$2"
  # tail -n +K skips the first K-1 entries; we want to delete entries beyond `keep`.
  # shellcheck disable=SC2012
  ls -1dt "$root/daily/"*/ 2>/dev/null \
    | tail -n +$((keep + 1)) \
    | xargs -r rm -rf
}
if [[ "$DEST" == *:* ]]; then
  ssh "${DEST%%:*}" bash -s -- "${DEST#*:}" "$KEEP_DAILY" <<'REMOTE'
root="$1"; keep="$2"
ls -1dt "$root/daily/"*/ 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -rf
REMOTE
else
  prune_local "$DEST" "$KEEP_DAILY"
fi

log "backup complete: $STAMP"
