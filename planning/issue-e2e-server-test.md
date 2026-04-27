Title: Rename E2E test to indicate server test and restrict notifications to admin only

Summary
-------
Rename the installation E2E alert so it clearly indicates it's a server-side test, and harden the test so alerts created during the install only notify the configured admin email (not the catch-all route or Slack).

Motivation
---------
During installs, the end-to-end test can create recipients and temporary alert rules that may inadvertently trigger broad notifications. We need to make the test non-disruptive and unmistakably identifiable when people see the alert.

Proposed Changes
---------------
- Prefix the E2E rule title with `[SERVER TEST]` so the subject is clearly an automated server probe.
- After creating the test rule, call alert-api PATCH `/api/alerts/{uid}/recipients` to set `notify_to` to the test recipient UID (admin email). This ensures per-alert routing and prevents catch-all/Slack delivery.
- Use `WATCHTOWER_NOTIFY_EMAIL` (from `.env`) as the default recipient in `install_alert_ui.sh` so installs target the configured admin address.

Files changed (implemented)
---------------------------
- testui/e2e_mail_test.py — rule title prefixed and per-alert recipients assigned via alert-api.
- install_alert_ui.sh — E2E invocation uses `WATCHTOWER_NOTIFY_EMAIL` if present.

Acceptance criteria
-------------------
- The E2E alert rule title contains `[SERVER TEST]`.
- The test alert's notify_to label is set to the admin contact UID only.
- When the test runs, Slack does not receive the test alert and only the admin inbox (or configured test recipient) is targeted.
- Default behaviour unchanged when `WATCHTOWER_NOTIFY_EMAIL` is not set (falls back to previous address).

Risks and mitigations
---------------------
- If the alert-api or Grafana provisioning APIs fail, the test may not be able to set notify_to; mitigation: log and continue with cleanup, do not block install.
- If the admin contact is already auto-subscribed, per-alert notify_to still prevents catch-all routes from firing for that alert.

Follow-ups
----------
- Optionally add a CLI flag or env var to entirely skip E2E by default in unattended installs.
- Add an integration test that validates the notify_to assignment path works as expected.
