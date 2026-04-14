# testui — Email Routing Test Scripts

Scripts for diagnosing and verifying the Grafana alert email delivery pipeline.

## Quick start (diagnosis first)

```bash
export GF_ADMIN_USER=admin
export GF_ADMIN_PASSWORD=secret
export SENDER_APP_PASSWORD='gmail-app-password'   # for inbox checks

python3 testui/diag.py          # inspect everything, report issues
```

---

## 0) `diag.py` — Comprehensive routing diagnostic  ← start here

Fetches live Grafana state, simulates the expected notification policy, diffs
the two, and shows which receivers each alert would actually reach.

**Sections printed:**
1. Contact points (addresses, `singleEmail` flag, placeholder detection)
2. Auto-subscribe settings (from Grafana annotation store)
3. Alert rules → route coverage (which receivers fire for each alert)
4. Live notification policy (routes with matchers)
5. Expected policy (simulated rebuild from `rebuild_notification_policy` logic)
6. Policy drift (differences between live and expected)
7. Issues summary (numbered, actionable)

**Run (inspect only):**
```bash
python3 testui/diag.py \
  --grafana-url http://localhost:3000 \
  -u "$GF_ADMIN_USER" -p "$GF_ADMIN_PASSWORD"
```

**Fix drifted policy + verify:**
```bash
# Push the expected policy directly to Grafana:
python3 testui/diag.py --rebuild

# Send a one-shot test email to all recipients:
python3 testui/diag.py --test-send

# Do both at once:
python3 testui/diag.py --rebuild --test-send
```

**Reset a long-running alert's notification state:**
```bash
# Disable/re-enable forces a fresh firing event + new notification window.
# Use this when an alert has been firing for hours and may be in repeat-suppression.
python3 testui/diag.py --fire-cycle "Dodo test 9"
```

**Verbose (print full policy JSON):**
```bash
python3 testui/diag.py --verbose
```

Exit codes: `0` = no issues, `1` = issues found, `2` = auth/connection error.

---

## 1) `setup_sender_recipient.py` — Configure sender as a recipient

Ensures `alerts.wanglab@gmail.com` exists in recipient list, is auto-subscribed,
and is assigned to the target alert (default: `Dodo test 9`).

```bash
python3 testui/setup_sender_recipient.py \
  --api-url http://localhost:8000/api \
  --username "$GF_ADMIN_USER" \
  --password "$GF_ADMIN_PASSWORD" \
  --recipient-name "alerts.wanglab@gmail.com" \
  --recipient-email "alerts.wanglab@gmail.com" \
  --alert-title "Dodo test 9"
```

---

## 2) `check_sender_inbox.py` — Verify inbox delivery

Logs into `alerts.wanglab@gmail.com` via IMAP and searches for alert text in
recent messages.  Use after `--test-send` or `--fire-cycle` to confirm delivery.

```bash
python3 testui/check_sender_inbox.py \
  --email alerts.wanglab@gmail.com \
  --query "TestAlert-20260413" \   # use the alert_name from diag.py --test-send output
  --since-minutes 10
```

For a real alert fire cycle, query by alert title:
```bash
python3 testui/check_sender_inbox.py \
  --query "Dodo test 9" \
  --since-minutes 10
```

Exit codes: `0` = match found, `1` = no match, `2` = missing credentials.

---

## Standard diagnostic workflow

```
1. diag.py                  → see full state + issues list
2. diag.py --rebuild        → fix policy drift (if any)
3. diag.py --test-send      → send one-shot email
4. check_sender_inbox.py    → confirm inbox delivery for test send
5. diag.py --fire-cycle ... → reset long-running alert notification state
6. check_sender_inbox.py    → confirm inbox delivery for real alert
```

## Environment variables

| Variable              | Used by                              |
|-----------------------|--------------------------------------|
| `GF_ADMIN_USER`       | diag.py, setup_sender_recipient.py   |
| `GF_ADMIN_PASSWORD`   | diag.py, setup_sender_recipient.py   |
| `GRAFANA_URL`         | diag.py (default: http://localhost:3000) |
| `ALERT_API_URL`       | diag.py (default: http://localhost:8000/api) |
| `SENDER_APP_PASSWORD` | check_sender_inbox.py                |
| `SENDER_EMAIL`        | check_sender_inbox.py (default: alerts.wanglab@gmail.com) |

## Known issues / caveats

- **`singleEmail=True`**: if any email contact point has `singleEmail=True`,
  all recipients get one grouped email instead of individual ones. The `diag.py`
  contact-points section will flag this. Fix via `POST /api/recipients/sync-email-format`.

- **Policy drift after Grafana restart**: if Grafana is restarted with a
  file-provisioned policy, the API-managed policy may be overwritten.
  `diag.py` will detect this as drift.

- **Repeat-suppression for long-firing alerts**: Alertmanager will not re-send
  within `repeat_interval` (currently 4h). If an alert has been firing for a
  long time, use `--fire-cycle` to reset it.

- **`lab-slack` / `lab-email`**: these are provisioned receivers.  If they
  disappear from Grafana, routing will fail silently.  `diag.py` checks for them.
