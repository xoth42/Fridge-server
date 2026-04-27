Date: 2026-04-20

Summary
-------
I reviewed installer and alert-related files to ensure a safe re-install/rebuild
flow and applied minimal changes so `./install.sh` can be run on a live system
without accidentally notifying users.

Actions taken
-------------
- Modified `testui/e2e_mail_test.py`:
  - Prefixed rule title with `[SERVER TEST]` (previous change).
  - Assigns `notify_to` to the created test recipient so alerts go only to that
    contact point (previous change).
  - Now explicitly disables `auto_subscribe` for the transient test recipient
    (new) so it won't be part of catch-all routes.
- Modified `install_alert_ui.sh`:
  - E2E recipient default now uses `WATCHTOWER_NOTIFY_EMAIL` from `.env` if set
    (change made earlier).
- Modified `install.sh`:
  - E2E test is skipped by default during automated installs; enable it by
    setting `RUN_E2E=true` in `.env` or invoking `RUN_E2E=true ./install.sh`.

Why these changes
-----------------
- The E2E test creates a firing rule and a recipient; previously this could
  deliver notifications to many recipients if the policy included catch-all
  recipients. Assigning `notify_to` and disabling `auto_subscribe` prevents
  the test alert from reaching Slack or broad mailing lists.
- Skipping the E2E by default makes `./install.sh` safe to run on live
  systems; maintainers can opt-in to running the full E2E when desired.

Files changed
-------------
- testui/e2e_mail_test.py
- install_alert_ui.sh
- install.sh
- planning/issue-e2e-server-test.md (created earlier)

Next steps / Recommendations
---------------------------
- Commit & push these changes and run a dry install on a staging host:

  ```bash
  ./install.sh   # will skip E2E by default
  # To run E2E intentionally:
  RUN_E2E=true ./install.sh
  ```

- Optionally, add a CI or manual checklist that runs the E2E test only in a
  controlled environment (staging) where admin inbox is monitored.
- Consider an explicit `--run-e2e` flag to `install.sh` for clarity.

Status
------
Changes applied locally and saved in planning/; ready to commit and push.
