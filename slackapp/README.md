Slack Alert Bot — /alerts

Overview
- Adds a simple Slack slash command `/alerts` that posts a Slack-formatted view
  of the "All Alerts" table from the Fridge Alert UI.
- The handler is implemented inside the `alert-api` service at the route
  `/api/slack/commands` and expects Slack's signed slash-command requests.

Setup
1. Create a Slack App in your workspace and enable a Slash Command:
   - Command: `/alerts`
   - Request URL: `https://<your-host>/alerts/api/slack/commands`
     (If your alert-api is mounted under `/alerts` as in the UI, include that
     prefix; otherwise use the direct host path to the FastAPI server.)
2. In the Slack App settings, copy the "Signing Secret" and set this env var
   for the `alert-api` service: `SLACK_SIGNING_SECRET`.
3. Ensure the deployed `alert-api` process has network access to Grafana and
   Prometheus (same as the UI does). The handler reuses the server's Grafana
   client and Prometheus queries.

Notes
- The slash command handler verifies Slack request signatures and posts the
  formatted alert list back to Slack via the supplied `response_url`.
- The response is posted as an ephemeral (user-only) message and lists up to
  40 alerts to avoid oversized messages.
- No extra Python packages are required; the handler uses existing deps
  (httpx, FastAPI) and standard libraries (`hmac`, `hashlib`, `urllib`).

Local testing
- To test locally, expose the server with `ngrok` or similar and point the
  Slack Request URL there. Make sure `SLACK_SIGNING_SECRET` is set locally.

If you want, I can:
- Add a small unit test or a convenience script to post a signed test payload.
- Make the Slack response public (`in_channel`) instead of ephemeral.
