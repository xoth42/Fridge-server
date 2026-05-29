"""End-to-end alert firing and email delivery test (pytest version).

Verifies the full path: metric pushed → Prometheus scrapes → Grafana rule
fires → Alertmanager routes → Mailpit receives the email.

Uses Mailpit's REST API instead of IMAP, so it works with zero external
credentials. Requires MAILPIT_URL to be set (provided by ci-e2e.yml).

The original standalone e2e_mail_test.py is kept for manual production runs
against the real server (IMAP path).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from client import NetClient

# ── Constants matching the standalone test ────────────────────────────────────

PROMETHEUS_DS_UID = "P1809F7CD0C75ACF3"
TEST_RULE_GROUP = "install-e2e-group"
TEST_INSTANCE = "install-e2e-probe"
TEST_JOB = "install-e2e-test"
TEST_METRIC = "ch1_t_kelvin"
TEST_METRIC_VALUE = 9999.0
TEST_METRIC_THRESHOLD = 0.0
TEST_FOLDER_TITLE = "Install E2E"
TEST_EVAL_INTERVAL_S = 10

_FAST_POLICY = {"group_wait": "5s", "group_interval": "30s", "repeat_interval": "4h"}
_PROD_POLICY = {"group_wait": "10s", "group_interval": "2m", "repeat_interval": "4h"}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _push_metric(pushgateway_url: str, value: float) -> None:
    url = (
        f"{pushgateway_url.rstrip('/')}/metrics"
        f"/job/{urllib.parse.quote(TEST_JOB)}"
        f"/instance/{urllib.parse.quote(TEST_INSTANCE)}"
    )
    body = f"# TYPE {TEST_METRIC} gauge\n{TEST_METRIC} {value}\n"
    req = urllib.request.Request(
        url=url, method="POST",
        headers={"Content-Type": "text/plain"},
        data=body.encode(),
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def _delete_pushgateway_group(pushgateway_url: str) -> None:
    url = (
        f"{pushgateway_url.rstrip('/')}/metrics"
        f"/job/{urllib.parse.quote(TEST_JOB)}"
        f"/instance/{urllib.parse.quote(TEST_INSTANCE)}"
    )
    try:
        urllib.request.urlopen(
            urllib.request.Request(url=url, method="DELETE"), timeout=10
        )
    except Exception:
        pass


def _get_or_create_folder(gf: NetClient, title: str) -> str:
    folders = gf.get("/api/folders")
    if isinstance(folders, list):
        for f in folders:
            if f.get("title") == title:
                return str(f["uid"])
    result = gf.post("/api/folders", {"title": title})
    return str(result.get("uid"))  # type: ignore[union-attr]


def _wait_for_rule_label(gf: NetClient, rule_uid: str, key: str, expected: str, timeout: int = 10) -> None:
    """Poll until a Grafana rule label reaches the expected value.

    Grafana's provisioning API is eventually consistent — a GET immediately
    after POST may return stale data. Waiting here prevents a stale policy
    rebuild from leaving the catch-all unguarded and emailing everyone.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rule = gf.get(f"/api/v1/provisioning/alert-rules/{rule_uid}")
            if isinstance(rule, dict) and rule.get("labels", {}).get(key) == expected:
                return
        except Exception:
            pass
        time.sleep(0.5)
    # Non-fatal: warn and continue — policy rebuild may be slightly stale.
    print(f"  [WARN] Rule {rule_uid} label {key!r} not confirmed within {timeout}s — proceeding anyway.")


def _mailpit_has_message(mailpit_url: str, subject_fragment: str, since: datetime.datetime) -> bool:
    """Return True if Mailpit has received a message matching subject_fragment after `since`."""
    url = f"{mailpit_url.rstrip('/')}/api/v1/messages"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return False
    for msg in data.get("messages", []):
        subject = msg.get("Subject", "") or ""
        received_str = msg.get("Created", "")
        if subject_fragment.lower() not in subject.lower():
            continue
        # Parse received time if available to filter old messages.
        if received_str:
            try:
                received = datetime.datetime.fromisoformat(received_str.replace("Z", "+00:00"))
                if received < since:
                    continue
            except Exception:
                pass
        return True
    return False


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fast_policy(gf_client: NetClient):
    """Temporarily shorten Alertmanager policy timing so the test completes quickly."""
    original = gf_client.get("/api/v1/provisioning/policies")
    try:
        patched = dict(original)  # type: ignore[arg-type]
        patched.update(_FAST_POLICY)
        patched.pop("provenance", None)
        gf_client.put(
            "/api/v1/provisioning/policies",
            patched,
            extra_headers={"X-Disable-Provenance": "true"},
        )
        yield
    finally:
        try:
            restore = dict(original)  # type: ignore[arg-type]
            restore.update(_PROD_POLICY)
            restore.pop("provenance", None)
            gf_client.put(
                "/api/v1/provisioning/policies",
                restore,
                extra_headers={"X-Disable-Provenance": "true"},
            )
        except Exception:
            pass


@pytest.fixture
def e2e_recipient(api_client: NetClient, gf_client: NetClient):
    """Ensure a test recipient exists; yield its (uid, contact_point_name); clean up."""
    created_uid: str | None = None
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    name = f"e2e-test-{ts}"
    result = api_client.post("/recipients", {"name": name, "email": f"{name}@ci.local"})
    uid = str(result.get("uid"))  # type: ignore[union-attr]
    created_uid = uid
    # Keep auto_subscribe=False so the catch-all doesn't fire to unrelated recipients.
    api_client.patch(f"/recipients/{uid}/auto-subscribe", {"auto_subscribe": False})

    # Resolve the Grafana contact point name (used to assert email routing).
    cps = gf_client.get("/api/v1/provisioning/contact-points")
    cp_name = next(
        (str(cp.get("name", "")) for cp in cps if cp.get("uid") == uid),  # type: ignore[union-attr]
        name,
    )

    yield uid, cp_name

    try:
        api_client.patch(f"/recipients/{created_uid}/auto-subscribe", {"auto_subscribe": True})
        api_client.delete(f"/recipients/{created_uid}")
    except Exception:
        pass


@pytest.fixture
def e2e_rule(gf_client: NetClient, api_client: NetClient, e2e_recipient, pushgateway_url: str):
    """Create a test Grafana rule wired to the e2e recipient; push metric; clean up."""
    recipient_uid, _cp_name = e2e_recipient
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    rule_title = f"[CI-E2E] mail-{ts}"

    folder_uid = _get_or_create_folder(gf_client, TEST_FOLDER_TITLE)

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
                    "type": "reduce", "expression": "A", "refId": "B",
                    "reducer": "last", "settings": {"mode": "dropNN"},
                },
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "type": "threshold", "expression": "B", "refId": "C",
                    "conditions": [{"evaluator": {"type": "gt", "params": [TEST_METRIC_THRESHOLD]}}],
                },
            },
        ],
        "for": "0s",
        "noDataState": "NoData",
        "execErrState": "Error",
        "labels": {
            "severity": "warning",
            "fridge": TEST_INSTANCE,
            "managed_by": "ci-e2e-test",
            "rulename": rule_title,
            "notify_to": recipient_uid,
        },
        "annotations": {"summary": f"CI e2e probe: {rule_title}"},
    }

    result = gf_client.post(
        "/api/v1/provisioning/alert-rules",
        payload,
        extra_headers={"X-Disable-Provenance": "true"},
    )
    rule_uid = str(result.get("uid"))  # type: ignore[union-attr]

    # Wait for Grafana to reflect the notify_to label before rebuilding policy.
    _wait_for_rule_label(gf_client, rule_uid, "notify_to", recipient_uid)

    # Rebuild policy so the per-recipient route exists before the alert fires.
    try:
        api_client.post("/policy/rebuild", None)
    except Exception:
        pass

    # Set fast eval interval on the test rule group.
    group_path = (
        f"/api/v1/provisioning/folder/{folder_uid}"
        f"/rule-groups/{urllib.parse.quote(TEST_RULE_GROUP)}"
    )
    try:
        group = gf_client.get(group_path)
        if isinstance(group, dict):
            group["interval"] = TEST_EVAL_INTERVAL_S
            group.pop("provenance", None)
            for rule in group.get("rules", []):
                for field in ("id", "provenance", "updated"):
                    rule.pop(field, None)
            gf_client.put(group_path, group, extra_headers={"X-Disable-Provenance": "true"})
    except Exception:
        pass

    # Push the metric that will trigger the rule.
    _push_metric(pushgateway_url, TEST_METRIC_VALUE)

    yield rule_uid, rule_title

    # Teardown: delete rule and remove test metric from Pushgateway.
    try:
        gf_client.delete(f"/api/v1/provisioning/alert-rules/{rule_uid}")
    except Exception:
        pass
    _delete_pushgateway_group(pushgateway_url)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.getenv("MAILPIT_URL"),
    reason="MAILPIT_URL not set — skipping e2e mail test (run via ci-e2e workflow)",
)
def test_alert_fires_and_email_delivered(
    gf_client: NetClient,
    api_client: NetClient,
    mailpit_url: str,
    fast_policy,
    e2e_rule,
) -> None:
    """Full path: metric pushed → alert fires → Alertmanager → Mailpit inbox.

    This test is skipped unless MAILPIT_URL is set, so it only runs in the
    ci-e2e tier (not ci-integration).
    """
    rule_uid, rule_title = e2e_rule
    test_start = datetime.datetime.now(datetime.timezone.utc)

    # Wait for the alert to reach 'firing' state in Grafana.
    fired = False
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            data = gf_client.get("/api/prometheus/grafana/api/v1/rules")
            if isinstance(data, dict):
                for group in data.get("data", {}).get("groups", []):
                    for rule in group.get("rules", []):
                        if rule.get("name") == rule_title:
                            raw = rule.get("state", "")
                            state = "normal" if raw == "inactive" else raw
                            if state.lower() == "firing":
                                fired = True
                            elif state.lower() == "error":
                                pytest.fail(
                                    f"Alert rule entered 'error' state — "
                                    "Prometheus may be unreachable or PromQL invalid."
                                )
        except Exception:
            pass
        if fired:
            break
        time.sleep(5)

    assert fired, f"Alert '{rule_title}' did not reach 'firing' within 90s."

    # Mailpit delivers nearly instantly — poll briefly for the message.
    delivered = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _mailpit_has_message(mailpit_url, rule_title, test_start):
            delivered = True
            break
        time.sleep(3)

    assert delivered, (
        f"Email for '{rule_title}' not found in Mailpit within 60s. "
        f"Check Alertmanager routing and Grafana SMTP config (pointing to mailpit:1025)."
    )
