#!/usr/bin/env bash
# =============================================================================
# nightly-backup.sh — hot backup of the fridge-server compose stack.
# =============================================================================
# Three modes (selected by flags; see --help):
#
#   default (no flags)        nightly hardlink-rotated backup of volumes + repo
#                             to $BACKUP_DEST/daily/<stamp>/, $BACKUP_DEST/current
#                             symlink to latest. Designed for fridge-backup.timer.
#
#   --data-only               same as default but skip the repo tree. Use when
#                             the repo is tracked in git and only data needs
#                             backing up. Still uses $BACKUP_DEST/daily/<stamp>/.
#
#   --data-only --push        one-shot data push to $BACKUP_DEST/migration/
#                             <stamp>/, $BACKUP_DEST/migration/CURRENT symlink
#                             to latest. Hardlinks dedupe against the previous
#                             CURRENT so repeat pushes are cheap. Intended for
#                             feeding a fresh VPS during migration.
#
#   --data-only --pull        fetch $BACKUP_DEST/migration/CURRENT/ into the
#                             local docker volume mountpoints. DESTRUCTIVE:
#                             stops the local stack, overwrites volume data,
#                             restarts. The mirror image of --push.
#
# Per-volume backup strategy (used for default, --data-only, --push):
#   prometheus-data   : POST /api/v1/admin/tsdb/snapshot, then rsync snapshot
#   grafana-data      : sqlite3 .backup grafana.db (online backup API), rsync;
#                       mirrors source uid/gid/mode so restore preserves uid 472
#   alertmanager-data : docker pause -> rsync -> docker unpause
#   *                 : plain rsync (idempotent / restart-tolerant data)
#
# Env overrides:
#   COMPOSE_DIR, BACKUP_DEST, BACKUP_SSH_KEY, KEEP_DAILY, KEEP_MIGRATION, LOCK
# =============================================================================
set -Eeuo pipefail
shopt -s nullglob

usage() {
  cat >&2 <<EOF
Usage:
  $(basename "$0") [--data-only] [--push|--pull] [COMPOSE_DIR] [DEST]
  $(basename "$0") --help

Modes:
  (none)                  nightly hardlink-rotated backup → DEST/daily/<stamp>/
  --data-only             same, but skip the repo tree
  --data-only --push      data-only snapshot → DEST/migration/<stamp>/ (+ CURRENT)
  --data-only --pull      restore DEST/migration/CURRENT/ into local volumes

Args (optional positionals):
  COMPOSE_DIR   compose dir to back up (default: \$COMPOSE_DIR or /opt/fridge-server)
  DEST          rsync target — "/abs/path" or "user@host:/abs/path"
                (default: \$BACKUP_DEST or /var/backups/fridge)

Env:
  COMPOSE_DIR, BACKUP_DEST, BACKUP_SSH_KEY, KEEP_DAILY, KEEP_MIGRATION, LOCK
EOF
}

# ── arg parsing ──────────────────────────────────────────────────────────────
DATA_ONLY=0
PUSH=0
PULL=0
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-only) DATA_ONLY=1 ;;
    --push)      PUSH=1 ;;
    --pull)      PULL=1 ;;
    -h|--help)   usage; exit 0 ;;
    --)          shift; POSITIONAL+=("$@"); break ;;
    -*)          echo "unknown flag: $1" >&2; usage; exit 2 ;;
    *)           POSITIONAL+=("$1") ;;
  esac
  shift
done
set -- "${POSITIONAL[@]}"

COMPOSE_DIR="${1:-${COMPOSE_DIR:-/opt/fridge-server}}"
DEST="${2:-${BACKUP_DEST:-/var/backups/fridge}}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_MIGRATION="${KEEP_MIGRATION:-3}"
LOCK="${LOCK:-/var/lock/fridge-backup.lock}"

# Validate flag combinations.
(( PUSH && !DATA_ONLY )) && { echo "--push requires --data-only" >&2; exit 2; }
(( PULL && !DATA_ONLY )) && { echo "--pull requires --data-only" >&2; exit 2; }
(( PUSH && PULL ))       && { echo "--push and --pull are mutually exclusive" >&2; exit 2; }

# Optional SSH identity for a remote $DEST (rsync-over-ssh nightly target).
# Empty → use root's default ~/.ssh identities. Ignored for a local $DEST.
BACKUP_SSH_KEY="${BACKUP_SSH_KEY:-}"
SSH_CMD="ssh"
SFTP_CMD="sftp"
if [[ -n "$BACKUP_SSH_KEY" ]]; then
  [[ -r "$BACKUP_SSH_KEY" ]] || { printf '[%s] FATAL: BACKUP_SSH_KEY %s not readable\n' \
       "$(date +%FT%T)" "$BACKUP_SSH_KEY" >&2; exit 1; }
  SSH_CMD="ssh -i $BACKUP_SSH_KEY -o IdentitiesOnly=yes"
  SFTP_CMD="sftp -i $BACKUP_SSH_KEY -o IdentitiesOnly=yes"
fi
# Restricted-shell remotes (notably rsync.net) reject `ssh user@host '<cmd>'`
# for anything beyond rsync/scp/sftp, so all remote metadata ops (mkdir,
# symlink update, retention) go through sftp batches instead.
# Manjaro/Arch ship fs.protected_regular=1 — root cannot O_CREAT|O_TRUNC a
# regular file in a sticky world-writable dir (e.g. /tmp) if it's owned by
# a different user. Avoid /tmp for the lock; /var/lock or /run is safe.
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

RSYNC_OPTS=(-aHAX --numeric-ids --delete)

# ── --pull mode (early exit) ─────────────────────────────────────────────────
# Completely different control flow: stops the local stack, fetches data from
# the remote CURRENT into local volume mountpoints, restarts the stack. No
# snapshot, no manifest, no rotation. Discovery uses `docker volume inspect`
# directly (containers don't have to be running first).
if (( PULL )); then
  PROJECT="$(docker compose config --format json | jq -r '.name')"
  [[ -n "$PROJECT" && "$PROJECT" != "null" ]] || die "compose project name not resolved"
  log "pull: project=$PROJECT  src=$DEST/migration/CURRENT/  → local volumes"

  # Confirm remote source exists before we tear anything down.
  if [[ "$DEST" == *:* ]]; then
    $SSH_CMD "${DEST%%:*}" "test -d '${DEST#*:}/migration/CURRENT'" \
      || die "remote $DEST/migration/CURRENT/ does not exist (run --push on the source host first)"
  else
    [[ -d "$DEST/migration/CURRENT" ]] \
      || die "local $DEST/migration/CURRENT/ does not exist"
  fi

  # Ensure volumes exist (idempotent: builds images on first run, creates
  # the docker volumes empty if absent, does not start containers).
  log "pull: ensuring volumes exist (docker compose create)"
  docker compose create >/dev/null

  declare -A PULL_VOL_HOST
  for vol in prometheus-data grafana-data alertmanager-data caddy_data caddy_config; do
    dvol="${PROJECT}_${vol}"
    if ! docker volume inspect "$dvol" >/dev/null 2>&1; then
      log "skip $vol (no docker volume $dvol — not declared in compose?)"
      continue
    fi
    mp=$(docker volume inspect "$dvol" -f '{{.Mountpoint}}')
    PULL_VOL_HOST["$vol"]="$mp"
    log "pull target $vol -> $mp"
  done
  [[ ${#PULL_VOL_HOST[@]} -gt 0 ]] || die "pull: no volumes resolved"

  log "pull: stopping stack"
  docker compose down

  for vol in "${!PULL_VOL_HOST[@]}"; do
    mp="${PULL_VOL_HOST[$vol]}"
    src_path="$DEST/migration/CURRENT/volumes/$vol/"
    log "pull rsync $vol  ($src_path → $mp/)"
    if [[ "$DEST" == *:* ]]; then
      host="${DEST%%:*}"; remote_src="${DEST#*:}/migration/CURRENT/volumes/$vol/"
      rsync "${RSYNC_OPTS[@]}" -e "$SSH_CMD" \
            "$host:$remote_src" "$mp/" \
        || die "pull rsync failed for $vol"
    else
      [[ -d "$src_path" ]] || { log "skip $vol: $src_path missing in remote snapshot"; continue; }
      rsync "${RSYNC_OPTS[@]}" "$src_path" "$mp/" \
        || die "pull rsync failed for $vol"
    fi
  done

  log "pull: starting stack"
  docker compose up -d

  log "pull complete: data restored from $DEST/migration/CURRENT/"
  exit 0
fi

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
mkdir -p "$STAGE/volumes"
(( DATA_ONLY )) || mkdir -p "$STAGE/repo"

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
  local dst_dir="$STAGE/volumes/grafana-data"
  mkdir -p "$dst_dir"

  # Grafana 11.x official image (alpine-based) does NOT ship sqlite3. Use the
  # host's sqlite3 against the volume's _data dir directly. The SQLite online
  # backup API is filesystem-based, not container-bound; it takes shared locks
  # and is safe to run concurrently with the live Grafana writer (WAL mode).
  log "grafana: sqlite3 .backup (host-side, online backup API)"
  if command -v sqlite3 >/dev/null; then
    sqlite3 "file:$host_path/grafana.db?mode=ro" \
            ".backup '$dst_dir/grafana.db'" \
        || die "host sqlite3 .backup failed against $host_path/grafana.db"
  else
    # Fallback: throwaway alpine container with sqlite installed at runtime.
    log "grafana: no host sqlite3; using throwaway alpine container"
    docker run --rm \
        -v "$host_path:/src:ro" \
        -v "$dst_dir:/dst" \
        alpine:3 sh -c \
        'apk add --no-cache sqlite >/dev/null \
         && sqlite3 file:/src/grafana.db?mode=ro ".backup /dst/grafana.db"' \
        || die "alpine fallback sqlite3 .backup failed"
  fi

  # sqlite3 .backup writes the destination file as whoever ran the script
  # (root, since we need sudo to read the source). That breaks the restore
  # — Grafana runs as uid 472 inside the container and can't write a
  # root-owned grafana.db, exits with "attempt to write a readonly
  # database" in a restart loop. Mirror the source file's uid/gid/mode
  # onto the backup copy so it survives the rsync to the new host.
  local src_meta
  src_meta="$(stat -c '%u %g %a' "$host_path/grafana.db")" \
    || die "could not stat source grafana.db for uid/gid replication"
  read -r _u _g _m <<< "$src_meta"
  chown "$_u:$_g" "$dst_dir/grafana.db"
  chmod "$_m"     "$dst_dir/grafana.db"
  log "grafana: backup file set to uid=$_u gid=$_g mode=$_m (matches source)"

  # Copy the rest of the volume tree (plugins, png renders, etc.) but exclude
  # the live DB files; the .backup'd copy in $dst_dir is canonical.
  # --delete with --exclude preserves the excluded grafana.db on the dest side.
  rsync "${RSYNC_OPTS[@]}" \
        --exclude='grafana.db' \
        --exclude='grafana.db-wal' \
        --exclude='grafana.db-shm' \
        "$host_path/" "$dst_dir/"
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
# Skipped under --data-only: the repo is tracked in git on $COMPOSE_DIR, and
# .env is the only thing not in git — duplicating it into every snapshot is
# noise once the repo is reproducible from upstream.
if (( ! DATA_ONLY )); then
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
else
  log "repo: skipped (--data-only)"
fi

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
# Layout differs by mode:
#   default / --data-only    →  $DEST/daily/<stamp>/    + $DEST/current
#   --data-only --push       →  $DEST/migration/<stamp>/ + $DEST/migration/CURRENT
#
# The "current" / "CURRENT" symlink doubles as the --link-dest for the next
# run, giving hardlink dedupe of unchanged blocks between runs.
if (( PUSH )); then
  SUBDIR="migration"
  PTR_NAME="CURRENT"
  KEEP="$KEEP_MIGRATION"
else
  SUBDIR="daily"
  PTR_NAME="current"
  KEEP="$KEEP_DAILY"
fi

ship() {
  local stage="$1" dest="$2" subdir="$3" ptr="$4"
  if [[ "$dest" == *:* ]]; then
    local host="${dest%%:*}" path="${dest#*:}"
    # Pre-create dir tree via sftp (works on restricted shells). The `-`
    # prefix tells sftp's batch mode to ignore "already exists" errors.
    $SFTP_CMD -b - "$host" >/dev/null <<EOF
-mkdir $path
-mkdir $path/$subdir
EOF
    rsync "${RSYNC_OPTS[@]}" \
          --link-dest="$path/$subdir/$ptr/" \
          -e "$SSH_CMD" \
          "$stage/" "$host:$path/$subdir/$STAMP/"
    # Atomically replace the pointer symlink. sftp's `symlink` creates,
    # so rm any existing one first (tolerated if absent on first run).
    $SFTP_CMD -b - "$host" >/dev/null <<EOF
-rm $path/$subdir/$ptr
symlink $STAMP $path/$subdir/$ptr
EOF
  else
    mkdir -p "$dest/$subdir"
    rsync "${RSYNC_OPTS[@]}" \
          --link-dest="$dest/$subdir/$ptr/" \
          "$stage/" "$dest/$subdir/$STAMP/"
    ln -sfn "$STAMP" "$dest/$subdir/$ptr"
  fi
}
log "shipping to $DEST/$SUBDIR/$STAMP  (pointer: $PTR_NAME)"
ship "$STAGE" "$DEST" "$SUBDIR" "$PTR_NAME"

# ── prune ────────────────────────────────────────────────────────────────────
# Prune entries under $DEST/$SUBDIR/ beyond $KEEP, skipping the $PTR_NAME
# symlink itself (matches by trailing slash on the listing).
prune_remote() {
  local host="$1" path="$2" subdir="$3" ptr="$4" keep="$5"
  # No reliable cross-vendor way to recursively `rm -rf` a directory tree
  # via sftp alone (the protocol is per-file), and restricted-shell hosts
  # block `ssh user@host 'rm -rf …'`. Skip remote retention entirely and
  # rely on the rsync-server provider's snapshot policy (rsync.net offers
  # FreeSnaps; check your provider for equivalents). Manual cleanup is
  # always possible via `sftp` for individual files / `rmdir` for empty
  # dirs. Local retention is unaffected (see prune_local).
  log "remote prune: skipped (rely on provider snapshots — keep=$keep would have applied here)"
  return 0
}

prune_local() {
  local root="$1" subdir="$2" ptr="$3" keep="$4"
  cd "$root/$subdir" 2>/dev/null || return 0
  # shellcheck disable=SC2012
  ls -1dt */ 2>/dev/null \
    | grep -v "^${ptr}/$" \
    | tail -n +$((keep + 1)) \
    | xargs -r rm -rf
}

if [[ "$DEST" == *:* ]]; then
  prune_remote "${DEST%%:*}" "${DEST#*:}" "$SUBDIR" "$PTR_NAME" "$KEEP"
else
  prune_local  "$DEST"       "$SUBDIR" "$PTR_NAME" "$KEEP"
fi

log "backup complete: $STAMP"
