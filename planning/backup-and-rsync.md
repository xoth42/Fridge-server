# Backup & rsync robustness

Companion doc to [`nightly-backup.sh`](../nightly-backup.sh). The script
lives in the repo root and is committed (commits `5c76983 add backup file`,
`f7642d8` / `b953087 update to nightly`). This file captures the *why* —
the design rationale, the gotchas that shaped it, and the systemd glue
that runs it on the production host. Do not let this drift; if the
script changes, update this doc.

## Scope: nightly only

This doc is **only** about the recurring backup. The one-time VPS
relocation is a separate flow with a different endpoint (the new host
itself, not the backup server), different encryption rules (mandatory
`age`), and a different runbook — see
[`vps-migration.md`](vps-migration.md). Both share the same `nightly-backup.sh`
as the snapshot primitive; everything *around* it differs:

| | Nightly | One-time migration |
|---|---|---|
| Endpoint | external rsync server, `$BACKUP_DEST` in `.env` | the new VPS host (per-run, on the command line) |
| Trigger | `fridge-backup.timer` | manual |
| Transit security | rsync-over-ssh, no payload encryption | mandatory `age` (the tarball contains `.env`) |
| Retention | `KEEP_DAILY` hardlink rotation | single tarball, shredded after restore |
| Restore expectation | almost never — disaster recovery only | exactly once |

Don't conflate them. In particular, never repoint `BACKUP_DEST` at the
new VPS to "migrate" — that gives you a continuously-mutating rsync
target on a host that isn't yet quiesced, with cleartext secrets
in motion.

## Why these per-volume strategies

Three of the named volumes hold open state that plain rsync tears:

| Volume              | Hazard                                                   | Primitive used                                                 |
|---------------------|----------------------------------------------------------|----------------------------------------------------------------|
| `prometheus-data`   | mmap'd head block + rotating WAL; raw rsync = corrupt DB | `POST /api/v1/admin/tsdb/snapshot` → hardlink dir → rsync      |
| `grafana-data`      | live SQLite at `grafana.db` (WAL mode)                   | SQLite online backup API (`sqlite3 .backup`) run host-side     |
| `alertmanager-data` | tiny but silences + nflog must be consistent             | `docker pause` → rsync → `docker unpause`                      |
| `caddy_data/_config`| restart-tolerant (LE certs + runtime state)              | plain rsync                                                    |
| repo tree           | text + config                                            | plain rsync, excludes `.git/`, caches, mounted data dirs       |

Rotation: `rsync --link-dest=<previous>` hardlinks unchanged blocks into
each daily snapshot. ~500 MB-class data × 7 dailies is fine on plain
`--link-dest`. If we ever need cross-snapshot dedupe across hosts,
switch to restic or borg — not yet justified.

## Hard-won gotchas

These all bit us at least once; the script's current shape is a direct
response. Don't undo them without re-reading the failure mode.

1. **Volume discovery by name-guessing fails.** Existing docker volumes
   may carry a legacy project prefix (`Fridge-server_`) from before a
   `docker compose -p` change or a directory rename. Solution: walk
   `docker inspect <cid> --format '{{range .Mounts}}...'` on every
   running compose container and read the real `Name` + `Source`. The
   short key used for dispatch strips *both* the current project prefix
   and the legacy capitalised one.

2. **Running without sudo silently skipped everything.** Volume `_data`
   dirs are root-owned. The `[[ -d "$host_path" ]]` discovery check
   passed for root but failed for the user, leaving zero volumes
   detected and the script "succeeded" with an empty backup. Always
   invoke via `sudo -E ./nightly-backup.sh`; the systemd unit runs as
   root which sidesteps this.

3. **Grafana 11 image has no `sqlite3`.** The official image is
   alpine-based and omits the sqlite3 binary. SQLite's online backup
   API is filesystem-based (not container-bound) so we run it host-side
   against the volume's `_data/grafana.db`. Fallback path: throwaway
   `alpine:3` container with `apk add sqlite`. Keep both code paths —
   not every host will have host-side sqlite3.

4. **`fs.protected_regular=1` blocks /tmp lock files.** Manjaro/Arch
   default. Even root cannot `O_CREAT|O_TRUNC` a regular file in a
   sticky world-writable dir if it's owned by another user. Lock
   defaults to `/var/lock/fridge-backup.lock`, not `/tmp`.

5. **First-run `--link-dest arg does not exist` is benign.** No prior
   `current/` symlink. Subsequent runs hardlink properly. Don't
   "fix" this with a guard — it'd hide a real misconfiguration later.

6. **WAL/SHM files must be excluded from the Grafana rsync.**
   `grafana.db-wal` / `grafana.db-shm` contain pages not yet
   checkpointed. The `.backup` produces a fully-checkpointed copy; the
   live WAL/SHM are stale at restore time and corrupt the DB if
   carried over.

7. **`sqlite3 .backup` writes the destination as the invoking user.**
   The script runs as root (sudo, to read root-owned `_data/` dirs), so
   the backup `grafana.db` lands as `root:root` mode `0644`. On the
   restore host, the rsync preserves that uid; the container's grafana
   user (uid 472) can't write its own DB; Grafana hits "attempt to
   write a readonly database" during provisioning, exits, restart loop
   forever. The script `stat`s the source DB after `.backup` and mirrors
   uid/gid/mode onto the backup copy so this never bites. If you ever
   see "readonly database" in restored Grafana logs, the fix is:
   ```
   sudo chown -R 472:0 /var/lib/docker/volumes/<project>_grafana-data/_data
   sudo find ... -type f -name 'grafana.db*' -exec chmod 0640 {} \;
   ```

8. **`git pull` after editing executable bit produces a mode merge
   conflict.** Stash → pull → drop stash; or commit the mode change.

## systemd unit + timer

Lives outside the repo on the production host (installed by hand once
per machine). Repeat on the new VPS during migration.

The unit loads `BACKUP_DEST`, `KEEP_DAILY`, and `BACKUP_SSH_KEY` from
`/opt/fridge-server/.env` via `EnvironmentFile=`. This means: change
`.env`, the next nightly run picks it up — no unit edit, no
`daemon-reload`.

`/etc/systemd/system/fridge-backup.service`:
```
[Unit]
Description=fridge-server nightly backup
Wants=docker.service
After=docker.service network-online.target
ConditionPathExists=/opt/fridge-server/docker-compose.yml
ConditionPathExists=/opt/fridge-server/.env

[Service]
Type=oneshot
User=root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
Environment=COMPOSE_DIR=/opt/fridge-server
EnvironmentFile=/opt/fridge-server/.env
ExecStart=/usr/local/sbin/nightly-backup.sh
ProtectSystem=strict
ReadWritePaths=/var/tmp /var/lock /var/lib/docker
PrivateTmp=true
NoNewPrivileges=true
```

Notes:
- `EnvironmentFile=` reads `KEY=VALUE` lines; the existing `.env` is
  already in that form. Quoting follows systemd rules (close enough to
  shell for our values; no command substitution).
- `ReadWritePaths` deliberately omits a local backup dir because
  production uses a remote `BACKUP_DEST`. If you set a local path, add
  it back: `ReadWritePaths=/var/backups/fridge /var/tmp /var/lock /var/lib/docker`.
- `BACKUP_SSH_KEY` is read inside the script and passed to `ssh`/`rsync`
  via `-i`. If unset, root's default identities are used.

`/etc/systemd/system/fridge-backup.timer`:
```
[Unit]
Description=Nightly fridge-server backup
Documentation=https://github.com/xoth42/Fridge-server

[Timer]
OnCalendar=*-*-* 03:17:00
RandomizedDelaySec=30min
Persistent=true
Unit=fridge-backup.service

[Install]
WantedBy=timers.target
```

Install + enable:
```
sudo install -m 0755 nightly-backup.sh /usr/local/sbin/
sudo install -m 0644 fridge-backup.service /etc/systemd/system/
sudo install -m 0644 fridge-backup.timer   /etc/systemd/system/
sudo mkdir -p /var/backups/fridge && sudo chmod 700 /var/backups/fridge
sudo systemctl daemon-reload
sudo systemctl enable --now fridge-backup.timer
systemctl list-timers fridge-backup.timer
```

First-install smoke test (remote `$BACKUP_DEST`):
```
sudo systemctl start fridge-backup.service
sudo journalctl -u fridge-backup.service -e --no-pager
# Confirm the snapshot landed on the backup host:
ssh "$BACKUP_HOST" "ls -lh $BACKUP_PATH/current/MANIFEST.txt $BACKUP_PATH/daily/ | head"
ssh "$BACKUP_HOST" "cat $BACKUP_PATH/current/MANIFEST.txt"
```

## External rsync server setup (one-time, per backup host)

The nightly target is an external rsync-over-ssh endpoint configured
through `BACKUP_DEST` in `.env` (form: `user@host:/abs/path`). One-time
prep on the backup host and the production host:

1. **Pick the backup user + path.** Suggested:
   `fridge-backup@<backup-host>:/srv/backups/fridge`. The user only
   needs an empty home + the target dir writeable.

2. **Generate a dedicated key on the production host** (don't reuse the
   admin's personal key):
   ```
   sudo ssh-keygen -t ed25519 -N '' -C 'fridge-backup' -f /root/.ssh/fridge-backup
   sudo cat /root/.ssh/fridge-backup.pub
   ```

3. **Install the public key on the backup host** with a restricted
   forced command — the key should only ever drive rsync, never an
   interactive shell:
   ```
   # On the backup host, in fridge-backup's ~/.ssh/authorized_keys:
   command="rrsync /srv/backups/fridge",restrict ssh-ed25519 AAAA... fridge-backup
   ```
   `rrsync` (ships with the rsync package) chroots the session to
   `/srv/backups/fridge`. `restrict` blocks port forwarding, agent
   forwarding, PTY allocation, and X11. If `rrsync` is unavailable,
   `command="rsync --server ..."` works but is harder to lock down —
   prefer `rrsync`.

4. **Prime known_hosts** as root on the production host (the systemd
   unit runs as root and won't have an interactive prompt to accept
   a new host key):
   ```
   sudo -u root ssh-keyscan -H <backup-host> | sudo tee -a /root/.ssh/known_hosts
   ```

5. **Point `.env`** at the new endpoint:
   ```
   BACKUP_DEST=fridge-backup@<backup-host>:/srv/backups/fridge
   KEEP_DAILY=7
   BACKUP_SSH_KEY=/root/.ssh/fridge-backup
   ```

6. **Dry-run before enabling the timer:**
   ```
   sudo -E env $(grep -E '^(BACKUP_DEST|KEEP_DAILY|BACKUP_SSH_KEY)=' /opt/fridge-server/.env) \
        /usr/local/sbin/nightly-backup.sh
   ```
   On success the backup host should show
   `/srv/backups/fridge/daily/<stamp>/` and a `current` symlink.

If rotating the key, replace both `BACKUP_SSH_KEY` (production) and
`authorized_keys` (backup host) in one window; the script fails closed
when the key is unreadable.

## What the manifest tells you on restore

`MANIFEST.txt` in each snapshot records:
- timestamp + source hostname
- compose dir + resolved project name
- discovered volumes with host paths and `du -sh` sizes
- `docker compose ps` snapshot of running containers
- `docker compose images` with sha256 digests, for reproducible restore
  if you ever need to pin to the same images that produced the data

If a restore behaves oddly, diff the manifest's image digests against
what's running.

## Numeric UIDs to preserve

`rsync -aHAX --numeric-ids` is non-negotiable for the volumes — service
uids inside containers don't map to host users. Expected uids in the
tarball:

- `0/0`       — root-owned files (caddy, alertmanager, repo metadata)
- `1000/1000` — repo tree (the deploying user)
- `472/0`     — Grafana runtime user (`472` is grafana inside the image)
- `65534/65534` — Prometheus (`nobody`) inside its container

Never `chown -R` the backup tree — it will brick the restore. If you
need to verify a backup as the user, use `sudo cat`/`sudo ls`, not
`chown`.

## Open follow-ups

- [ ] **Pick the actual backup host** and fill `BACKUP_DEST` in `.env`
      on the production server. Until that's done, the timer will fail
      against the placeholder value in `.env.example`.
- [ ] Decide retention beyond `KEEP_DAILY` — weekly/monthly GFS rungs
      currently not implemented; the script's `prune_local` would need
      a small extension.
- [ ] Restic/borg evaluation only if we add a second backup target
      and hardlink dedupe stops being enough.
- [ ] Alert if `systemctl list-timers fridge-backup.timer` shows a
      missed firing window (host was down). Cheap: a Prometheus
      `node_systemd_unit_state` scrape + Grafana alert rule.
- [ ] Periodic restore drill — pull `/srv/backups/fridge/current/`
      down to a throwaway VM quarterly and run the verification
      commands from [`vps-migration.md`](vps-migration.md#1-snapshot-on-the-source-host).
      Untested backups age into theatre.
