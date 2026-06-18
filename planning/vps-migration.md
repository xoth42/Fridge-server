# VPS migration runbook

Move the live fridge-server stack from its current host (local Arch /
Manjaro stand-in) to an external VPS. Companion to
[`backup-and-rsync.md`](backup-and-rsync.md) — that doc covers the
nightly mechanism; this doc covers the one-time relocation that uses
the same primitives.

This runbook is provider-agnostic on purpose. Host OS provisioning and
VPS choice are out of scope; we resume once the new host has Docker
Engine + Compose plugin + jq + envsubst + ufw + age + ssh access.

## Overall shape

Four phases. Do them in order; do not parallelise phases 1↔2 (the
package step depends on the snapshot being complete and verified).

1. **Snapshot** on the source host using `nightly-backup.sh`.
2. **Package + integrity-stamp** the snapshot into one tarball.
3. **Encrypt + ship** to the new host (mandatory — `.env` ships every
   secret).
4. **Restore + start + verify** on the new host.

DNS cutover is deliberately out-of-band and discussed under
[*DNS-before-Caddy*](#dns-before-caddy) below — get it right before the
new host's Caddy tries to renew.

## 1. Snapshot on the source host

The nightly script *is* the migration snapshot mechanism — there's no
separate code path. Run it ad-hoc into a dedicated staging directory
so daily backups in `/var/backups/fridge` are not co-mingled with the
migration artifact:

```
cd /path/to/Fridge-server   # repo root on source
export REPO=$(pwd)
export STAGE=/path/to/migration-staging
export KEEP_DAILY=2
mkdir -p "$STAGE"
sudo -E /usr/local/sbin/nightly-backup.sh "$REPO" "$STAGE"
```

Verify with sudo — the tree is root-owned, **do not `chown` it**, the
per-service uids must be preserved:

```
sudo cat  "$STAGE/current/MANIFEST.txt"
sudo bash -c 'du -sh "$1/current/volumes/"*' _ "$STAGE"
sudo ls   -lh "$STAGE/current/volumes"
sudo file "$STAGE/current/volumes/grafana-data/grafana.db"
sudo sqlite3 "$STAGE/current/volumes/grafana-data/grafana.db" \
     'PRAGMA integrity_check; SELECT COUNT(*) FROM alert_rule;'
sudo ls "$STAGE/current/volumes/prometheus-data/" | head
sudo find "$STAGE/current/volumes/caddy_data" \
     \( -name '*.crt' -o -name '*.json' \) | head
```

Expected:
- `integrity_check: ok`, `alert_rule` count > 0
- prometheus-data has 21+ ULID block dirs (head + WAL + chunks)
- caddy_data has `acme/…letsencrypt…/certificates/<domain>/*.crt`

If any of those fail, stop. Re-run the snapshot and investigate before
packaging — packaging a broken snapshot only wastes uplink bandwidth.

## 2. Package + integrity-stamp

```
cd "$STAGE/daily"
SNAP=$(basename "$(readlink "$STAGE/current")")    # e.g. 2026-06-17_190930

sudo tar --xattrs --acls --numeric-owner --sort=name \
         -czf "$STAGE/fridge-migration-$SNAP.tar.gz" "$SNAP"
sudo chown $USER:$USER "$STAGE/fridge-migration-$SNAP.tar.gz"

sha256sum "$STAGE/fridge-migration-$SNAP.tar.gz" \
   | tee  "$STAGE/fridge-migration-$SNAP.tar.gz.sha256"

# UID/GID preservation check — expect numeric uids like 0/0, 472/0, 65534/65534.
tar -tvzf "$STAGE/fridge-migration-$SNAP.tar.gz" \
    | awk '{print $2}' | sort -u | head
```

`--numeric-owner` is critical: see the uid table in
[`backup-and-rsync.md`](backup-and-rsync.md#numeric-uids-to-preserve).
A symbolic-owner tarball will silently bind grafana/prometheus uids to
whatever happens to exist on the new host, then the containers will
refuse to read their own files.

## 3. Encrypt + ship

`.env` is inside the snapshot's `repo/` tree and contains SMTP creds,
DuckDNS token, name.com API token, Grafana admin password, Slack
webhook, signing secret, and the Grafana SA token. Treat the tarball
as a secret blob — never push it unencrypted over any wire.

```
# Passphrase mode (use a real password manager entry).
age -p -o "$STAGE/fridge-migration-$SNAP.tar.gz.age" \
       "$STAGE/fridge-migration-$SNAP.tar.gz"
shred -u   "$STAGE/fridge-migration-$SNAP.tar.gz"

sha256sum  "$STAGE/fridge-migration-$SNAP.tar.gz.age" \
   | tee   "$STAGE/fridge-migration-$SNAP.tar.gz.age.sha256"

# Round-trip pre-flight: should print the same digest as the .tar.gz.sha256
# created in phase 2. Confirms passphrase + age + tarball all match BEFORE
# we shred plaintext on the wire.
age -d "$STAGE/fridge-migration-$SNAP.tar.gz.age" | sha256sum
```

Ship (push direction — source-side SSH inbound rarely available from
behind home/lab NAT):

```
scp -p \
    "$STAGE/fridge-migration-$SNAP".tar.gz.age \
    "$STAGE/fridge-migration-$SNAP".tar.gz.age.sha256 \
    "$STAGE/fridge-migration-$SNAP".tar.gz.sha256 \
    user@new-host:/root/
```

Slow uplink? Swap for `rsync --info=progress2 -e ssh ...`. Resumable
and shows ETA — `scp` doesn't.

## 4. Receive, verify, restore on the new host

Order matters: create the named volumes empty *first*, then populate
them, then start the stack. Running `install.sh` first creates +
starts everything and you'd have to stop and clobber empty volumes —
extra work for no gain.

### 4a. Receive-side verification

```
cd /root
sha256sum -c fridge-migration-*.tar.gz.age.sha256    # transit integrity
age -d -o fridge-migration.tar.gz fridge-migration-*.tar.gz.age

# post-decryption integrity (against the recorded plaintext hash)
HASH=$(awk '{print $1}' fridge-migration-*.tar.gz.sha256)
echo "$HASH  fridge-migration.tar.gz" | sha256sum -c -

mkdir -p /root/restore
tar --xattrs --acls --numeric-owner -xzf fridge-migration.tar.gz -C /root/restore
shred -u fridge-migration.tar.gz                     # remove plaintext

ls /root/restore/        # should show <SNAP>/ with volumes/, repo/, MANIFEST.txt
sqlite3 /root/restore/*/volumes/grafana-data/grafana.db \
        'PRAGMA integrity_check; SELECT COUNT(*) FROM alert_rule;'
```

### 4b. Place repo

Use the snapshot's `repo/` tree as canonical — it carries the
snapshot-time `.env`, the generated `alertmanager.runtime.yml`, and any
local edits that hadn't reached git yet.

```
SNAP_DIR=/root/restore/<SNAP>
sudo mkdir -p /opt/fridge-server
sudo rsync -aHAX --numeric-ids "$SNAP_DIR/repo/" /opt/fridge-server/
sudo chmod 600 /opt/fridge-server/.env
```

Optional but recommended container cleanup at this point — both can be
done now or deferred:
- delete the `watchtower:` service (it's already monitor-only;
  replaced by [`fridge-pull.timer`](#optional-weekly-image-refresh)
  below).
- delete the `duckdns:` service and replace with a host cron (see
  *Operational notes* below).

### 4c. Materialize empty volumes, populate, start

```
cd /opt/fridge-server
sudo docker compose create        # creates network + named volumes, starts nothing

# Same volume-discovery technique the backup script uses.
PROJECT="$(docker compose config --format json | jq -r '.name')"
for vol in prometheus-data grafana-data alertmanager-data caddy_data caddy_config; do
  dvol="${PROJECT}_$vol"
  mp=$(sudo docker volume inspect "$dvol" -f '{{.Mountpoint}}')
  echo "$vol -> $mp"
  sudo rsync -aHAX --numeric-ids --delete \
       "$SNAP_DIR/volumes/$vol/" "$mp/"
done

sudo docker compose up -d
sudo docker compose ps

for u in :9090/-/ready :9091/-/healthy :9093/-/healthy :3000/api/health :8000/api/health; do
  printf '%-22s ' "$u"; curl -fsS http://localhost$u && echo
done
```

If any of the five health endpoints don't return 200, stop and read
the offending container's logs. Don't fall back to running
`install.sh` — it'll regenerate config and overwrite the restored
state.

## DNS-before-Caddy

The restored `caddy_data` carries the existing Let's Encrypt cert bound
to `fridge.zickers.us`. LE certs are 90 days; the cert remains valid
until expiry regardless of host. But:

- The cert was issued via DNS-01 through name.com, so renewal does
  **not** require port 80 on the new host.
- However, `<duckdns-subdomain>.duckdns.org` must already resolve to
  the **new** host's public IP before Caddy attempts renewal.
- `fridge.zickers.us` is a CNAME → DuckDNS subdomain → public IP.

Cutover order:
1. Update DuckDNS to point at the new public IP (host cron or one-shot
   `curl 'https://www.duckdns.org/update?domains=<SUB>&token=<TOKEN>&ip='`).
2. Wait for propagation (usually <5 min for DuckDNS).
3. Verify on new host: `dig <SUB>.duckdns.org` and
   `dig fridge.zickers.us` both resolve to the new IP.
4. Then bring up the stack.

If the existing cert has under ~10 days left, update DNS *first* even
if you're not migrating yet — a missed renewal interrupts HTTPS.

## Operational notes for the new host

### Pushgateway exposure

`0.0.0.0:9091` is intentionally public, protected by ufw allow/deny
rules. Confirm after first start:

```
sudo ufw status numbered
# Expected order (deny must come AFTER allow):
# [1] 9091/tcp ALLOW IN <ALLOWED_PUSH_CIDR>
# [2] 9091/tcp DENY  IN Anywhere
```

`install.sh` installs these. If `ALLOWED_PUSH_CIDR` is empty in `.env`,
the allow rule is skipped — pushgateway will be public. The college's
CIDR was still pending from IT at last check; track that loose end.

### DuckDNS replacement as host cron

Saves an image + container, keeps DDNS updates independent of compose
restarts:

```
echo '*/5 * * * * root curl -fsS "https://www.duckdns.org/update?domains=<SUB>&token=<TOKEN>&ip=" >/dev/null' \
   | sudo tee /etc/cron.d/duckdns
sudo chmod 644 /etc/cron.d/duckdns
```

Then delete the `duckdns:` service from `docker-compose.yml`.

### Optional weekly image refresh

Cleaner than watchtower (which is monitor-only in this stack anyway —
`WATCHTOWER_MONITOR_ONLY=true`):

`/etc/systemd/system/fridge-pull.service`:
```
[Unit]
Description=Pull and restart fridge-server images
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/fridge-server
ExecStart=/usr/bin/docker compose pull
ExecStart=/usr/bin/docker compose up -d --remove-orphans
```

`/etc/systemd/system/fridge-pull.timer`:
```
[Unit]
Description=Weekly image refresh

[Timer]
OnCalendar=Mon 04:00
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

Then delete `watchtower:` from `docker-compose.yml`.

### Don't forget the nightly timer on the new host

The systemd unit + timer from
[`backup-and-rsync.md`](backup-and-rsync.md#systemd-unit--timer) must
be re-installed on the new host. Until that's done, the new host has
no backups.

## Rollback

The source host stack is *not* torn down by this runbook. If the new
host fails verification:
1. Leave the new host's compose `down`.
2. Roll DuckDNS back to the source host's public IP.
3. Keep the encrypted tarball + sha256 files for re-attempt.
4. The source host has been writing data the whole time (nothing on
   the new host was acknowledging metrics yet, since DNS routed to
   source) — no data loss.

Only after the new host has passed verification *and* run cleanly for
a few hours should the source host be retired.

## Checklist

- [ ] Source host has `nightly-backup.sh` installed and a recent
      successful run (check `journalctl -u fridge-backup.service`).
- [ ] New host: docker + jq + envsubst + ufw + age + ssh server.
- [ ] New host: public IP known; firewall allows 22, 8443, 9091 (rest
      127.0.0.1).
- [ ] DuckDNS token in hand for the DNS flip.
- [ ] Backup tarball encrypted, hash files written, plaintext shredded.
- [ ] Backup transferred + transit + post-decrypt hashes both pass.
- [ ] Repo placed at `/opt/fridge-server`, `.env` mode 600.
- [ ] `docker compose create` ran; volumes populated by uid-preserving
      rsync.
- [ ] All five health endpoints return 200.
- [ ] DuckDNS flipped; `fridge.zickers.us` resolves to new IP.
- [ ] HTTPS works at `https://fridge.zickers.us:8443`.
- [ ] Pushgateway reachable from the lab uploader network, blocked
      from the open internet.
- [ ] `fridge-backup.timer` enabled on new host.
- [ ] Source host retired (or left running as warm standby until next
      LE renewal proves the new host is self-sustaining).
