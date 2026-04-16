# Email Recipient System: Findings and Changes

Date: 2026-04-13/14
Scope: alert email delivery, recipient routing, auto-subscribe UX/backend, and one-shot diagnostics

## Executive Summary

We confirmed SMTP delivery to both real recipient addresses, fixed multiple UI/UX and backend issues, added a secure one-shot test endpoint, and unlocked/replaced Grafana's file-provisioned notification policy so recipient-based routing can be managed via API.

The system now supports:
- Per-recipient auto-subscribe control
- Recipient count visibility per alert
- One-shot test email to all recipients with unique numeric test IDs
- Admin-only access for the email-all check endpoint

## What We Learned

1. The "Grouped by" header in Grafana email is not controlled by the body template.
- Overriding ng_alert_notification changes body text only.
- The "Grouped by" block comes from Grafana's HTML wrapper/template and is not removed by normal provisioning template overrides.

2. The biggest delivery blocker was policy provenance, not SMTP.
- Active policy was file-provisioned (`provenance: file`) with old severity-only routes.
- API attempts to overwrite policy failed with 403 invalidProvenance.
- Result: new recipients existed but were not part of active routes for normal alerts.

3. SMTP path itself works for external recipients.
- Direct Grafana receiver tests to both addresses returned status ok.
- Confirmed user-observed inbox delivery for both:
  - isaac@pelenur.com
  - ipelenur@umass.edu

4. Transient API errors can still produce successful writes.
- UI toggles initially reverted on 503 even when backend state had changed.
- Required verify-after-error logic to avoid false rollback in UI.

## Backend Changes Made

### alert-api/grafana_client.py

1. Auto-subscribe persistence via Grafana annotations
- Added methods to read/write recipient auto-subscribe settings.
- Settings stored as JSON map keyed by contact UID.

2. Policy rebuild logic improvements
- Dynamic catch-all built from available email contact points.
- Auto-subscribe applied to catch-all recipient inclusion.
- Added defensive behavior for annotation-read failures (fail-open to defaults).

3. One-shot test notification support
- Added list_email_recipients and send_test_email_to_all_recipients.
- Uses Grafana endpoint:
  - POST /api/alertmanager/grafana/config/api/v1/receivers/test
- Sends to deduplicated real recipient addresses.
- Filters placeholder example addresses.

4. Numbered test identity for diagnostics
- Every test send includes:
  - test_id: UTC timestamp (YYYYMMDDHHMMSS)
  - alert_name: TestAlert-<test_id>
- Included in payload labels/summary and returned in API response.

5. Auth model hardening
- Added admin validation helper for sensitive operations.
- Added utility for using caller basic auth for selected Grafana calls.

### alert-api/main.py

1. Added recipient check endpoint
- POST /api/recipients/check
- Admin-only protection
- Returns test metadata including test_id, alert_name, addresses

2. Added/extended recipient controls
- PATCH /api/recipients/{uid}/auto-subscribe
- Recipient list includes auto_subscribe

3. Recipient-aware alert list
- list_alerts computes recipient_count
- Explicit notify_to list uses list length
- Empty notify_to uses auto-subscribe catch-all count

4. Rebuild hooks
- Recipient create/delete trigger policy rebuild attempts
- Assignment and auto-subscribe paths wired for rebuild attempts

## Frontend Changes Made

### alert-ui/index.html
- Added Recipients column header in alert table.
- Recipient config panel now includes auto-subscribe checkbox (moved out of list rows).
- Auto-subscribe control placed above assignment guidance text per user preference.

### alert-ui/app.js
- Added recipient_count display in alert rows.
- Added auto-subscribe checkbox behavior in recipient assignment panel.
- Implemented generalized resilient toggle helper for:
  - auto-subscribe toggle
  - per-alert recipient assignment checkboxes
- Behavior under transient errors:
  - disable + grey while pending
  - verify state after non-OK/exception
  - keep UI state if backend change actually landed
  - revert only on true failure

### alert-ui/style.css
- Widened main layout/container.
- Added styling for recipient count column.
- Added pending styles for checkbox update flows.
- Added assignment-panel auto-subscribe styles.

## Operational Diagnostics and Evidence

Key evidence files created under mytmp:
- mytmp/diag-policy-put-body.json
- mytmp/diag-policy-put-headers.txt
- mytmp/final-policy-active.json
- mytmp/final2b-check.json
- mytmp/test-ipelenur-result.json
- mytmp/test-isaac-result.json

Important observed responses:
- Policy overwrite failure prior to unlock:
  - 403 alerting.notifications.invalidProvenance
- Policy unlock step:
  - DELETE /api/v1/provisioning/policies -> 202 Accepted
- Final one-shot check:
  - sent true
  - recipient_count 2
  - addresses [isaac@pelenur.com, ipelenur@umass.edu]
  - alert_name TestAlert-<timestamp>

## Current Status

Working now:
- One-shot test sends to all real recipients
- Numeric test alert IDs for easy delivery trace-back
- Admin-gated check endpoint
- Auto-subscribe UI/behavior improvements
- Recipient count in alert table

Known caveat:
- "Grouped by" email header text is Grafana-wrapper behavior and not fully removable via standard template body override.

## Recommendations / Next Steps

1. Run one real (non-test) alert fire/resolve cycle and confirm inbox delivery per recipient.
2. Keep policy under API management (avoid reintroducing file-provisioned policy lock).
3. If complete removal of "Grouped by" is required, implement custom Grafana email template override at container/template level.
4. Optional: add a small UI action button for admin users to call /api/recipients/check directly from the recipients panel.

---

## Addendum: Current Status Update (Latest Session)

### New Work Completed

1. Policy unlock executed in Grafana
- `DELETE /api/v1/provisioning/policies` returned `202 Accepted` and reset to Grafana default policy.

2. Active routing policy explicitly installed
- Policy currently includes routes to:
  - `lab-slack`
  - `Isaac 2`
  - `Isaac ipelenur@umass.edu`
  - `lab-email`
- This replaced the old severity-only file policy path.

3. One-shot check endpoint behavior improved
- `POST /api/recipients/check` now emits numbered test alert names and filters placeholder/example addresses.
- Verified recent response shape:
  - `sent: true`
  - `recipient_count: 2`
  - `addresses: [isaac@pelenur.com, ipelenur@umass.edu]`
  - `alert_name: TestAlert-<timestamp>`

4. Test harness scripts added under `testui/`
- `testui/setup_sender_recipient.py`
  - Ensures `alerts.wanglab@gmail.com` exists in recipients.
  - Enables auto-subscribe (best effort).
  - Ensures target alert assignment (`Dodo test 9` by default).
- `testui/check_sender_inbox.py`
  - IMAP checker for `alerts.wanglab@gmail.com` inbox.
  - Searches for a provided alert query text in recent messages.
- `testui/README.md`
  - Usage and required env vars/flags.

### Latest Diagnostic Finding on Dodo test 9

For `Dodo test 9` at time of diagnosis:
- Alert state: firing
- `notify_to: []` (send-to-all semantics)
- API computed `recipient_count: 4` earlier due to all email contact points, then test-check confirms only real recipient addresses after filtering in check path.

Interpretation:
- Routing config and test delivery path are functional.
- Intermittent "one inbox got it, one did not" behavior is consistent with Alertmanager dedupe/repeat timing during continuous firing periods and route changes.

Mitigation executed:
- Performed disable/enable cycle on `Dodo test 9` to force a fresh notification lifecycle under current routes.

### Outstanding Risk / Gap

The one-shot check path is proven. The remaining reliability question is consistency for long-running real firing alerts across state transitions. This is an operational/timing behavior (dedupe/group/repeat), not a basic SMTP or address configuration failure.

### Immediate Next Validation Step

1. Trigger a controlled fire/resolve/fire cycle for `Dodo test 9` and capture timestamps from both inboxes.
2. Correlate with Grafana notifier logs and alert instance transitions.
3. If needed, tune `group_wait`, `group_interval`, or `repeat_interval` specifically for this route tree.
