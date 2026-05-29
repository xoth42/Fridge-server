#!/usr/bin/env python3
"""
End-to-end email delivery test for the fridge alert system.

Tests the FULL production routing path — NOT the /api/recipients/check endpoint
which uses Grafana's receiver-test API and bypasses Alertmanager routing entirely.

This test validates:
  1. A new recipient can be added via the alert-api (as the UI would do)
  2. The notification policy is correctly rebuilt to include that recipient
  3. A real Grafana alert rule fires when a test metric exceeds its threshold
  4. Alertmanager routes the alert through to the recipient's inbox

If step 4 fails but step 3 succeeds, the SMTP path is suspect (not policy routing).
If step 3 fails, the metric push or Grafana evaluation path is broken.
If step 2 fails, the SA token lacks Admin role or has invalid provenance.

Usage:
  export GF_ADMIN_USER=admin GF_ADMIN_PASSWORD=secret GMAIL_APP_PASSWORD=xxxx
  python3 testui/e2e_mail_test.py

Exits 0 on success, 1 on test failure, 2 on setup/config error.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import email as email_module
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv

# Must match PROMETHEUS_DS_UID in grafana_client.py and the provisioning datasource UID.
PROMETHEUS_DS_UID = "P1809F7CD0C75ACF3"

# Rule group used exclusively for E2E test rules.  Separate group avoids
# contaminating the user-alerts group timing during the test.
TEST_RULE_GROUP = "install-e2e-group"

# Instance and job labels used when pushing the test metric to Pushgateway.
# Must NOT collide with any real fridge instance label.
TEST_INSTANCE = "install-e2e-probe"
TEST_JOB = "install-e2e-test"

# A metric name that is in metrics.yml (so it is a real PromQL metric),
# queried with a synthetic instance label so it never collides with real data.
TEST_METRIC = "ch1_t_kelvin"
TEST_METRIC_VALUE = 9999.0
TEST_METRIC_THRESHOLD = 0.0  # fires whenever value > 0

# Grafana folder used for the test rule.
TEST_FOLDER_TITLE = "Install E2E"

# Policy timing applied during the test, then restored.
_FAST_POLICY = {
    "group_wait": "5s",
    "group_interval": "30s",
    "repeat_interval": "4h",
}
_PROD_POLICY = {
    "group_wait": "10s",
    "group_interval": "2m",
    "repeat_interval": "4h",
}

# Rule group evaluation interval during test (seconds).
_TEST_EVAL_INTERVAL = 10


# ─── HTTP client ──────────────────────────────────────────────────────────────


class _Client:
    """stdlib urllib-based HTTP client with Basic auth and JSON bodies."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 20) -> None:
        self.base = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._timeout = timeout

    def _req(
        self,
        method: str,
        path: str,
        payload: object = None,
        extra: dict | None = None,
    ) -> object:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers: dict[str, str] = {
            "Authorization": self._auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {path}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error {method} {path}: {exc}") from exc

    def get(self, path: str, params: dict | None = None) -> object:
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._req("GET", path)

    def post(self, path: str, payload: object = None, extra: dict | None = None) -> object:
        return self._req("POST", path, payload, extra)

    def put(self, path: str, payload: object = None, extra: dict | None = None) -> object:
        return self._req("PUT", path, payload, extra)

    def patch(self, path: str, payload: object = None, extra: dict | None = None) -> object:
        return self._req("PATCH", path, payload, extra)

    def delete(self, path: str) -> object:
        return self._req("DELETE", path)


# ─── Logging helpers ──────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}", file=sys.stderr, flush=True)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr, flush=True)


# ─── Pushgateway ──────────────────────────────────────────────────────────────


def _push_metric(pushgateway_url: str, metric: str, value: float, instance: str, job: str) -> None:
    url = f"{pushgateway_url.rstrip('/')}/metrics/job/{urllib.parse.quote(job)}/instance/{urllib.parse.quote(instance)}"
    body = f"# HELP {metric} E2E install test probe\n# TYPE {metric} gauge\n{metric} {value}\n"
    req = urllib.request.Request(
        url=url,
        method="POST",
        headers={"Content-Type": "text/plain"},
        data=body.encode(),
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"Pushgateway push returned HTTP {resp.status}")


def _delete_pushgateway_group(pushgateway_url: str, job: str, instance: str) -> None:
    url = f"{pushgateway_url.rstrip('/')}/metrics/job/{urllib.parse.quote(job)}/instance/{urllib.parse.quote(instance)}"
    req = urllib.request.Request(url=url, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # best effort


# ─── Grafana helpers ──────────────────────────────────────────────────────────


def _get_or_create_folder(gf: _Client, title: str) -> str:
    folders = gf.get("/api/folders", params={"limit": 100})
    if isinstance(folders, list):
        for f in folders:
            if f.get("title") == title:
                return str(f["uid"])
    result = gf.post("/api/folders", {"title": title})
    return str(result["uid"])  # type: ignore[index]


def _create_test_rule(gf: _Client, folder_uid: str, rule_title: str, notify_to_uid: str) -> str:
    """POST a Grafana alert rule that fires as soon as the test metric appears.

    IMPORTANT — notify_to_uid MUST be in the labels at creation, not patched
    afterward.  Grafana's unified-alerting evaluation scheduler caches the rule
    definition when it first loads the rule.  If notify_to is added in a second
    API call (PUT/PATCH after creation), there is a window — often 20-60s on a
    loaded system — where the scheduler is still using the original label set
    (no notify_to).  If the alert fires in that window, the notification policy's
    catch-all guard ("notify_to !~ '.+'") evaluates against an absent label,
    which Alertmanager treats as empty string.  Empty string does NOT match .+,
    so the guard is TRUE and the catch-all fires → Slack and all auto-subscribed
    recipients receive the test email.

    Apr 27th:
    Observed in Grafana 11.6.0: rule created at T+0, notify_to patched at T+0
    (same second), alert fired at T+21s — scheduler still had the pre-patch
    definition, Slack fired.  Baking notify_to in at creation eliminates the
    window entirely.
    """
    payload = {
        "title": rule_title,
        "ruleGroup": TEST_RULE_GROUP,
        "folderUID": folder_uid,
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": PROMETHEUS_DS_UID,
                "model": {
                    "expr": f'{TEST_METRIC}{{instance="{TEST_INSTANCE}"}}',
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "A",
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "type": "reduce",
                    "expression": "A",
                    "refId": "B",
                    "reducer": "last",
                    "settings": {"mode": "dropNN"},
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "type": "threshold",
                    "expression": "B",
                    "refId": "C",
                    "conditions": [
                        {"evaluator": {"type": "gt", "params": [TEST_METRIC_THRESHOLD]}}
                    ],
                },
            },
        ],
        "for": "0s",
        "noDataState": "NoData",
        "execErrState": "Error",
        "labels": {
            "severity": "warning",
            "fridge": TEST_INSTANCE,
            "managed_by": "install-e2e-test",
            "rulename": rule_title,
            "notify_to": notify_to_uid,
        },
        "annotations": {
            "summary": f"Install E2E test probe: {rule_title}",
        },
    }
    result = gf.post(
        "/api/v1/provisioning/alert-rules",
        payload,
        extra={"X-Disable-Provenance": "true"},
    )
    uid = result.get("uid")  # type: ignore[union-attr]
    if not uid:
        raise RuntimeError(f"Alert rule created but no uid returned: {result}")
    return str(uid)


def _set_rule_group_interval(gf: _Client, folder_uid: str, interval_seconds: int) -> None:
    """Read the test rule group and rewrite with a shorter eval interval."""
    path = f"/api/v1/provisioning/folder/{folder_uid}/rule-groups/{urllib.parse.quote(TEST_RULE_GROUP)}"
    try:
        group = gf.get(path)
        if not isinstance(group, dict):
            _warn(f"Unexpected rule group response type: {type(group)}")
            return
        group["interval"] = interval_seconds
        group.pop("provenance", None)
        # Strip read-only fields from each rule in the group before PUT.
        for rule in group.get("rules", []):
            for field in ("id", "provenance", "updated"):
                rule.pop(field, None)
        gf.put(path, group, extra={"X-Disable-Provenance": "true"})
        _log(f"Rule group eval interval set to {interval_seconds}s")
    except Exception as exc:
        _warn(f"Could not set rule group interval to {interval_seconds}s: {exc}")
        _warn(f"Alert may wait up to ~60s before first evaluation (Grafana default)")


def _patch_policy_timing(gf: _Client, timing: dict) -> None:
    """Read the current notification policy, update timing fields only, write back."""
    try:
        current = gf.get("/api/v1/provisioning/policies")
        if not isinstance(current, dict):
            _warn("Could not read current policy for timing patch")
            return
        current.update(timing)
        current.pop("provenance", None)
        gf.put(
            "/api/v1/provisioning/policies",
            current,
            extra={"X-Disable-Provenance": "true"},
        )
    except Exception as exc:
        _warn(f"Could not patch policy timing: {exc}")


def _wait_for_firing(gf: _Client, rule_title: str, timeout: int) -> bool:
    """Poll Grafana state endpoint until the test rule reaches 'firing'."""
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        try:
            data = gf.get("/api/prometheus/grafana/api/v1/rules")
            if isinstance(data, dict):
                for group in data.get("data", {}).get("groups", []):
                    for rule in group.get("rules", []):
                        if rule.get("name") == rule_title:
                            raw = rule.get("state", "")
                            state = "normal" if raw == "inactive" else raw
                            if state != last_state:
                                _log(f"Alert state: {state}")
                                last_state = state
                            if state.lower() == "firing":
                                return True
                            if state.lower() == "error":
                                # execErrState=Error means datasource issue
                                raise RuntimeError(
                                    f"Alert rule entered 'error' state — "
                                    "Prometheus may be unreachable or PromQL invalid"
                                )
        except RuntimeError:
            raise
        except Exception as exc:
            _warn(f"State poll error: {exc}")
        remaining = int(deadline - time.monotonic())
        if remaining > 0:
            _log(f"Waiting for 'firing'... ({remaining}s remaining)")
            time.sleep(5)
    return False


# ─── IMAP inbox check ─────────────────────────────────────────────────────────


def _decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out: list[str] = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            out.append(payload.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(payload))
    return "".join(out)


def _check_inbox(
    imap_host: str,
    imap_port: int,
    email_addr: str,
    password: str,
    query: str,
    since_dt: datetime.datetime,
    mailbox: str = "INBOX",
) -> bool:
    """Return True if a message matching *query* (case-insensitive) exists in the
    inbox and was received after *since_dt*."""
    needle = query.lower()
    mail = imaplib.IMAP4_SSL(imap_host, imap_port)
    mail.login(email_addr, password)
    try:
        status, _ = mail.select(mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open mailbox {mailbox!r}")

        status, data = mail.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return False

        ids = data[0].split()[-200:]  # only check the most recent 200 messages

        for msg_id in reversed(ids):
            status, fetched = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not fetched or fetched[0] is None:
                continue
            raw_bytes = fetched[0][1]
            if not isinstance(raw_bytes, (bytes, bytearray)):
                continue

            msg = email_module.message_from_bytes(raw_bytes)

            # Skip messages received before the test started.
            date_hdr = msg.get("Date")
            if date_hdr:
                try:
                    msg_dt = parsedate_to_datetime(date_hdr)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=datetime.timezone.utc)
                    else:
                        msg_dt = msg_dt.astimezone(datetime.timezone.utc)
                    if msg_dt < since_dt:
                        continue
                except Exception:
                    pass  # if we can't parse the date, don't skip

            subject = _decode_mime_header(msg.get("Subject", ""))

            if msg.is_multipart():
                parts: list[str] = []
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            parts.append(payload.decode(charset, errors="replace"))
                body = "\n".join(parts)
            else:
                raw_payload = msg.get_payload(decode=True) or b""
                body = raw_payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

            if needle in f"{subject}\n{body}".lower():
                _log(f"MATCH: Subject={subject!r}")
                return True

        return False
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()


# ─── Recipient management ─────────────────────────────────────────────────────


def _ensure_recipient(api: _Client, gf: _Client, email_addr: str) -> str:
    """Ensure email_addr exists as a recipient in the alert-api; return its UID.

    Checks Grafana contact points directly (so we can match by address, not
    just by name) before creating a new entry.
    """
    # Check via Grafana API (has the addresses field that alert-api list doesn't expose).
    try:
        cps = gf.get("/api/v1/provisioning/contact-points")
        if isinstance(cps, list):
            for cp in cps:
                if cp.get("type") != "email":
                    continue
                raw_addrs = (cp.get("settings") or {}).get("addresses", "")
                addrs = [
                    a.strip().lower()
                    for a in re.split(r"[;,\s]+", raw_addrs)
                    if a.strip() and "@" in a
                ]
                if email_addr.lower() in addrs:
                    uid = cp.get("uid", "")
                    _log(
                        f"Recipient {email_addr!r} already exists as contact point '{cp.get('name')}' (uid={uid})"
                    )
                    # Ensure auto_subscribe is enabled for E2E tests
                    try:
                        api.patch(f"/recipients/{uid}/auto-subscribe", {"auto_subscribe": True})
                        _log(f"Ensured auto_subscribe=true for recipient uid={uid}")
                    except Exception as exc:
                        _warn(f"Could not set auto_subscribe for uid={uid}: {exc}")
                    return uid
    except Exception as exc:
        _warn(f"Could not check existing contact points: {exc} — will try to create")

    _log(f"Adding recipient {email_addr!r} via alert-api...")
    result = api.post("/recipients", {"name": email_addr, "email": email_addr})
    uid = result.get("uid")  # type: ignore[union-attr]
    if not uid:
        raise RuntimeError(f"Recipient created but no uid in response: {result}")
    _log(f"Recipient created (uid={uid})")
    # Newly-created recipients default to auto_subscribe=true, but be explicit
    try:
        api.patch(f"/recipients/{uid}/auto-subscribe", {"auto_subscribe": True})
        _log(f"Set auto_subscribe=true for new recipient uid={uid}")
    except Exception as exc:
        _warn(f"Could not set auto_subscribe for new uid={uid}: {exc}")
    return str(uid)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="E2E email delivery test — validates full Alertmanager routing path",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-url", default=os.getenv("ALERT_API_URL", "http://localhost:8000/api"))
    parser.add_argument("--grafana-url", default=os.getenv("GRAFANA_URL", "http://localhost:3000"))
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", "http://localhost:9091"))
    parser.add_argument("-u", "--username", default=os.getenv("GF_ADMIN_USER"))
    parser.add_argument("-p", "--password", default=os.getenv("GF_ADMIN_PASSWORD"))
    parser.add_argument("--recipient-email", default=os.getenv("WATCHTOWER_NOTIFY_EMAIL", "alerts.wanglab@gmail.com"))
    parser.add_argument("--imap-host", default=os.getenv("SENDER_IMAP_HOST", "imap.gmail.com"))
    parser.add_argument("--imap-port", type=int, default=993)
    parser.add_argument(
        "--imap-email",
        default=os.getenv("SMTP_FROM", "alerts.wanglab@gmail.com"),
        help="Email account to log into for inbox verification (defaults to SMTP_FROM)",
    )
    parser.add_argument(
        "--imap-password",
        default=os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SENDER_APP_PASSWORD"),
        help="App password for --imap-email inbox",
    )
    parser.add_argument("--imap-mailbox", default="INBOX")
    parser.add_argument(
        "--fire-timeout",
        type=int,
        default=90,
        help="Seconds to wait for the alert to reach 'firing' state (default: 90)",
    )
    parser.add_argument(
        "--inbox-timeout",
        type=int,
        default=120,
        help="Seconds to wait for email inbox delivery (default: 120)",
    )
    parser.add_argument(
        "--skip-email-check",
        action="store_true",
        help="Skip IMAP check — verify alert fires but not inbox delivery",
    )
    args = parser.parse_args()

    if not args.username or not args.password:
        _fail("Missing Grafana credentials.  Set GF_ADMIN_USER / GF_ADMIN_PASSWORD.")
        return 2

    if not args.imap_password and not args.skip_email_check:
        _warn("GMAIL_APP_PASSWORD / SENDER_APP_PASSWORD not set — skipping inbox check.")
        args.skip_email_check = True

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    # Prefix with SERVER TEST so recipients who see this notification know
    # it is an automated server-side installation diagnostic.
    rule_title = f"[SERVER TEST] install-e2e-{ts}"
    test_start = datetime.datetime.now(datetime.timezone.utc)

    gf = _Client(args.grafana_url, args.username, args.password)
    api = _Client(args.api_url, args.username, args.password)

    # Verify both services are reachable before touching state.
    try:
        gf.get("/api/health")
    except RuntimeError as exc:
        _fail(f"Grafana unreachable at {args.grafana_url}: {exc}")
        return 2
    try:
        api.get("/health")
    except RuntimeError as exc:
        _fail(f"Alert-api unreachable at {args.api_url}: {exc}")
        return 2

    print("\n── E2E Mail Delivery Test ──")
    print(f"  Grafana     : {args.grafana_url}")
    print(f"  Alert-api   : {args.api_url}")
    print(f"  Pushgateway : {args.pushgateway_url}")
    print(f"  Recipient   : {args.recipient_email}")
    if not args.skip_email_check and args.imap_email.lower() != args.recipient_email.lower():
        print(f"  +CC verify  : {args.imap_email}  (IMAP check account)")
    print(f"  Rule title  : {rule_title}")
    if args.skip_email_check:
        print("  Inbox check : skipped (no IMAP credentials)")
    else:
        print(f"  Inbox check : {args.imap_email} via {args.imap_host} (timeout {args.inbox_timeout}s)")
    print()

    rule_uid: str | None = None

    try:
        # ── Step 1: Add recipient ─────────────────────────────────────────────
        # This also triggers rebuild_notification_policy in the alert-api, which
        # sets the policy with the new contact point in the catch-all routes.
        _log("Step 1/5: Ensuring recipient exists via alert-api...")
        # Keep the returned contact UID so we can assign this alert to that
        # recipient only (avoid catch-all and Slack routes).
        recipient_uid = _ensure_recipient(api, gf, args.recipient_email)

        # Newly-created or discovered recipients default to auto_subscribe=true
        # inside _ensure_recipient. For this transient test recipient we want
        # to avoid adding it to the catch-all list, so explicitly disable
        # auto_subscribe here (best-effort; non-fatal on error).
        try:
            _log("Disabling auto_subscribe for transient test recipient...")
            api.patch(f"/recipients/{recipient_uid}/auto-subscribe", {"auto_subscribe": False})
            _log(f"auto_subscribe=false set for uid={recipient_uid}")
        except Exception as exc:
            _warn(f"Could not set auto_subscribe=false for uid={recipient_uid}: {exc}")

        # If the IMAP check account differs from the primary recipient, also add
        # it as a per-alert recipient so the inbox check can actually find the
        # email.  (The IMAP account has valid credentials; the primary recipient
        # may not.)  Its auto_subscribe is left unchanged — it's a permanent
        # system address, not a transient test contact.
        imap_uid: str | None = None
        if not args.skip_email_check and args.imap_email.lower() != args.recipient_email.lower():
            _log(f"Ensuring IMAP check account '{args.imap_email}' is a recipient...")
            imap_uid = _ensure_recipient(api, gf, args.imap_email)

        # ── Step 2: Set fast policy timing ────────────────────────────────────
        # Must happen AFTER step 1 because create_recipient rebuilds the policy
        # with hardcoded (slow) timing that would overwrite our fast timing.
        _log("Step 2/5: Patching policy to fast timing for test...")
        _patch_policy_timing(gf, _FAST_POLICY)
        _log(f"  group_wait={_FAST_POLICY['group_wait']}  group_interval={_FAST_POLICY['group_interval']}")

        # ── Step 3: Push test metric to Pushgateway ───────────────────────────
        # Uses a synthetic instance label that can't collide with real fridge data.
        _log(f"Step 3/5: Pushing {TEST_METRIC}={TEST_METRIC_VALUE} for instance={TEST_INSTANCE!r}...")
        _push_metric(args.pushgateway_url, TEST_METRIC, TEST_METRIC_VALUE, TEST_INSTANCE, TEST_JOB)
        _log("  Metric pushed. Prometheus will scrape within ~15s.")

        # ── Step 4: Create test alert rule ────────────────────────────────────
        # Done via direct Grafana provisioning API (not alert-api) so we can set
        # for="0s" and use the synthetic instance label without validation constraints.
        _log("Step 4/5: Creating test alert rule in Grafana...")
        folder_uid = _get_or_create_folder(gf, TEST_FOLDER_TITLE)
        # Build the notify_to value: primary recipient + IMAP account (if
        # different) so the inbox check can verify delivery.
        # notify_to baked in at creation — see _create_test_rule docstring for
        # why patching it afterward caused Slack to fire in Grafana 11.6.0.
        notify_to_uids = [recipient_uid]
        if imap_uid and imap_uid != recipient_uid:
            notify_to_uids.append(imap_uid)
        notify_to_str = ",".join(notify_to_uids)
        rule_uid = _create_test_rule(gf, folder_uid, rule_title, notify_to_str)
        _log(f"  Rule created: uid={rule_uid} (notify_to={notify_to_str})")

        # Rebuild the notification policy now that the rule with notify_to exists,
        # so the per-recipient route and catch-all guard are applied immediately.
        try:
            _log("Rebuilding notification policy (per-recipient guard)...")
            api.post("/policy/rebuild", None)
            _log("  Policy rebuilt.")
        except Exception as exc:
            _warn(f"Could not rebuild policy: {exc}")

        # Shorten the rule group evaluation interval to 10s.
        # Default is 1 minute; without this the worst-case first-eval delay
        # added to the 15s scrape window could exceed our timeout budget.
        _log(f"  Setting rule group eval interval to {_TEST_EVAL_INTERVAL}s...")
        _set_rule_group_interval(gf, folder_uid, _TEST_EVAL_INTERVAL)

        # ── Step 5a: Wait for alert to reach 'firing' ─────────────────────────
        _log(f"Step 5/5: Waiting for alert to reach 'firing' state (timeout {args.fire_timeout}s)...")
        _log("  Timeline: ~15s Prometheus scrape → ~10s rule eval → fires")

        try:
            fired = _wait_for_firing(gf, rule_title, args.fire_timeout)
        except RuntimeError as exc:
            _fail(str(exc))
            return 1

        if not fired:
            _fail(f"Alert '{rule_title}' did not reach 'firing' within {args.fire_timeout}s.")
            _fail("Possible causes:")
            _fail("  - Prometheus has not yet scraped Pushgateway")
            _fail("  - Rule group eval interval was not shortened (still 60s default)")
            _fail("  - Prometheus datasource UID mismatch (check PROMETHEUS_DS_UID)")
            return 1

        _log(f"Alert '{rule_title}' is FIRING.")

        # ── Step 5b: Wait for inbox delivery ─────────────────────────────────
        if args.skip_email_check:
            _warn("Inbox check skipped — cannot confirm email delivery.")
            print(f"\n  [ OK ]  Alert fired. Email delivery NOT verified (no IMAP credentials).")
            print(f"  To verify manually: python3 testui/check_sender_inbox.py --query {rule_title!r} --since-minutes 5")
            return 0

        _log(f"Waiting for email in {args.imap_email} inbox (timeout {args.inbox_timeout}s)...")
        _log(f"  Searching for: {rule_title!r}")

        delivered = False
        deadline = time.monotonic() + args.inbox_timeout
        while time.monotonic() < deadline:
            try:
                found = _check_inbox(
                    args.imap_host,
                    args.imap_port,
                    args.imap_email,
                    args.imap_password,
                    rule_title,
                    since_dt=test_start,
                    mailbox=args.imap_mailbox,
                )
            except Exception as exc:
                _warn(f"IMAP error: {exc}")
                time.sleep(15)
                continue

            if found:
                delivered = True
                break

            remaining = int(deadline - time.monotonic())
            if remaining > 0:
                _log(f"No match yet — polling again in 10s ({remaining}s remaining)...")
                time.sleep(10)

        if delivered:
            print(f"\n  [ OK ]  Email for '{rule_title}' delivered to {args.recipient_email} (verified via {args.imap_email}).")
            return 0
        else:
            _fail(f"Email for '{rule_title}' NOT received at {args.recipient_email} within {args.inbox_timeout}s.")
            _fail("The alert fired and routing policy was set — this is an SMTP/delivery failure.")
            _fail("Check: Grafana SMTP settings, Alertmanager logs, Gmail spam folder.")
            _fail(f"Retry inbox check: python3 testui/check_sender_inbox.py --query {rule_title!r} --since-minutes 10")
            return 1

    finally:
        print("\n  Cleaning up test artifacts...")

        # Delete the test alert rule from Grafana.
        if rule_uid:
            try:
                gf.delete(f"/api/v1/provisioning/alert-rules/{rule_uid}")
                _log(f"Deleted test alert rule {rule_uid}")
            except Exception as exc:
                _warn(f"Could not delete test alert rule {rule_uid}: {exc}")

        # Delete the Pushgateway metric group so it doesn't pollute Prometheus.
        try:
            _delete_pushgateway_group(args.pushgateway_url, TEST_JOB, TEST_INSTANCE)
            _log("Deleted Pushgateway test metric group")
        except Exception as exc:
            _warn(f"Could not delete Pushgateway metric: {exc}")

        # Restore production policy timing.
        # The policy routes remain intact (recipient stays, which is correct).
        try:
            _patch_policy_timing(gf, _PROD_POLICY)
            _log(
                f"Restored production policy timing: "
                f"group_wait={_PROD_POLICY['group_wait']}  "
                f"group_interval={_PROD_POLICY['group_interval']}"
            )
        except Exception as exc:
            _warn(f"Could not restore policy timing: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
