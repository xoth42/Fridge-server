# mail-fail.md — Failed attempt log: isaac@pelenur.com not receiving alerts

## Symptom
After fresh install + adding isaac@pelenur.com as a recipient, only ipelenur@umass.edu
receives alert emails. Slack alert fires correctly.

---

## Attempt 1 — "SA is Editor, can't PUT provisioning policy"

### Theory
`rebuild_notification_policy` PUTs `/api/v1/provisioning/policies`, which requires
Admin role. The SA was created with `"role":"Editor"`. Every call from the UI 403'd.
The exception was swallowed by `except Exception: pass` in `create_recipient`, so the
notification policy was never updated after initial install. ipelenur@umass.edu worked
only because install.sh seeds that contact point into the *initial* policy using admin
curl — any recipient added later is invisible to the router.

### Changes made
- `install.sh`: SA creation `"role":"Editor"` → `"role":"Admin"`
- `grafana_client.py`: policy PUT always uses SA token headers (not user basic_auth)
- `main.py`: replaced `except Exception: pass` with `raise HTTPException(502)` in all
  four rebuild callers

### Why it still failed
The SA section in install.sh is double-guarded:

```bash
if [[ "$IS_CI" != "true" ]] && [[ -z "${GRAFANA_SA_TOKEN:-}" ]]; then
```

On a reinstall where `.env` persists with the existing token, the **entire section is
skipped**. The role change in the creation command never executes. On a fresh
install where `.env` is absent but Grafana data volume persists, the "already exists"
branch fires and also skips everything:

```bash
else
    ok "Grafana service account 'alert-api' already exists — skipping."
fi
```

So the SA remained Editor. The policy PUT continued to fail (now visibly — returning
502 to the UI — but the policy was still wrong).

### What was missed
The role enforcement needs to happen on every install run, independent of whether
the token already exists in .env. The fix to the SA creation command was correct but
unreachable in the common reinstall path.
