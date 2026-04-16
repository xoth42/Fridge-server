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

---

## Attempt 2 — "Policy bootstrap overwrites all per-recipient routes"

### Theory
`install_alert_ui.sh` step 3 calls `_apply_policy` which PUTs `INITIAL_POLICY` — a
bare two-route policy with only `lab-slack` and `lab-email`.  This runs on every
install and **wipes any per-recipient routes** that the alert-api had previously
written.

After the bootstrap, the E2E test calls `_ensure_recipient`, but only triggers a
policy rebuild if it *creates* a new contact point.  If `alerts.wanglab@gmail.com`
already exists, `_ensure_recipient` returns early without calling
`rebuild_notification_policy`.  So the policy stays at the bare INITIAL_POLICY with
no per-recipient routes for the rest of the install run.

Isaac's contact point exists in Grafana (was added via the UI), but after every
install the policy forgets his route, and nothing in the old install flow restored it.

### Changes made

**`alert-api/main.py`** — new `POST /api/policy/rebuild` endpoint:
- Admin-protected; calls `rebuild_notification_policy` with all current alert rules.
- Idempotent — reads all existing contact points and rewrites full routing policy.
- Returns `{"rebuilt": true}` on success, 502 on Grafana error.

**`install_alert_ui.sh`** — two new sections inserted between steps 3 and 4:

*Step 3b "Notification policy repair"*
- Calls `POST http://localhost:8000/api/policy/rebuild` with Grafana admin Basic auth.
- Runs unconditionally after the policy bootstrap so per-recipient routes are always
  restored, regardless of whether any new recipients were added this run.
- Warns but does not die on failure (E2E test that follows will catch the outcome).

*Step "Notification routing policy verification"* (the test)
- Fetches the live policy and all contact points from Grafana via the SA token.
- jq query: finds every `type=email` contact point with a non-empty uid that is NOT
  the default policy receiver (`lab-email`), then checks if its name appears in any
  `routes[].receiver` in the policy.
- `die()`s with the list of missing names if any are absent.
- This is the hard gate that would have caught isaac's missing route on every install.

### Why this works
`rebuild_notification_policy` in `grafana_client.py` reads all contact points,
builds catch-all routes for every auto-subscribe email contact point (defaulting to
True for all), and PUTs the full policy using the SA token (Admin role confirmed in
step 1).  The per-recipient routes for explicitly-assigned alerts are also restored
from the `notify_to` labels on existing alert rules.

### Remaining risk
If a recipient has `auto_subscribe=False` (stored in a Grafana annotation), they will
NOT appear in the catch-all routes after a rebuild.  This is intentional behaviour —
they only receive alerts explicitly assigned to them via `notify_to`.  The
verification test skips this check; explicit-route coverage would require querying
the annotations store.
