"""Tests for the auto_subscribe recipient exclusion behaviour.

Verifies that recipients with auto_subscribe=False are NOT included in the
notification policy catch-all routes after a new alert is created.

Root cause being tested: create_alert previously did not call
rebuild_notification_policy(), so Grafana fell back to its default routing
(all contact points), ignoring auto_subscribe settings on recipients.
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from client import NetClient


# ── Helpers ───────────────────────────────────────────────────────────────────


def _catch_all_receiver_names(gf: NetClient) -> set[str]:
    """Return receiver names in the policy routes that fire for un-assigned alerts.

    Catch-all routes are identified by having no matchers, or by carrying the
    guard matcher "notify_to !~ .+" which the alert-api uses to exclude alerts
    that already have an explicit per-recipient assignment.
    """
    policy = gf.get("/api/v1/provisioning/policies")
    names: set[str] = set()
    for route in policy.get("routes", []):  # type: ignore[union-attr]
        matchers = route.get("object_matchers", [])
        is_catch_all = not matchers or any(
            m[0] == "notify_to" and m[1] == "!~" for m in matchers
        )
        if is_catch_all:
            names.add(route.get("receiver", ""))
    return names


def _contact_point_name(gf: NetClient, uid: str) -> str:
    cps = gf.get("/api/v1/provisioning/contact-points")
    for cp in cps:  # type: ignore[union-attr]
        if cp.get("uid") == uid:
            return str(cp.get("name", ""))
    return ""


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def opted_out_recipient(api_client: NetClient, gf_client: NetClient):
    """Yield (uid, cp_name) for a recipient with auto_subscribe=False.

    Prefers an existing opted-out recipient to avoid side-effects. If none
    exists, creates a temporary one and deletes it after the test.
    """
    created_uid: str | None = None
    original_auto_subscribe: bool | None = None
    target_uid: str | None = None

    recipients = api_client.get("/recipients")
    for r in recipients:  # type: ignore[union-attr]
        if not r.get("auto_subscribe", True) and not r.get("provisioned", False):
            target_uid = str(r["uid"])
            original_auto_subscribe = False
            break

    if target_uid is None:
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        temp_name = f"test-auto-sub-{ts}"
        result = api_client.post(
            "/recipients",
            {"name": temp_name, "email": f"{temp_name}@example.invalid"},
        )
        target_uid = str(result.get("uid"))  # type: ignore[union-attr]
        created_uid = target_uid
        original_auto_subscribe = True
        api_client.patch(
            f"/recipients/{target_uid}/auto-subscribe",
            {"auto_subscribe": False},
        )

    cp_name = _contact_point_name(gf_client, target_uid)
    assert cp_name, f"Could not resolve contact point name for uid={target_uid}"

    yield target_uid, cp_name

    # ── Teardown ──────────────────────────────────────────────────────────────
    if created_uid:
        try:
            api_client.patch(
                f"/recipients/{created_uid}/auto-subscribe",
                {"auto_subscribe": True},
            )
            api_client.delete(f"/recipients/{created_uid}")
        except RuntimeError:
            pass
    elif original_auto_subscribe is True:
        # We found an existing opted-out recipient and didn't change it, so no
        # restore needed. (original_auto_subscribe=True means we created a temp.)
        pass


@pytest.fixture
def test_alert(api_client: NetClient, test_fridge: str, test_metric: str):
    """Create a harmless test alert and delete it after the test."""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    result = api_client.post(
        "/alerts",
        {
            "name": f"[TEST] auto-sub-{ts}",
            "fridge": test_fridge,
            "metric": test_metric,
            "operator": ">",
            "threshold": 9999.0,
        },
    )
    uid = str(result.get("uid"))  # type: ignore[union-attr]

    yield uid

    try:
        api_client.delete(f"/alerts/{uid}")
    except RuntimeError:
        pass


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_opted_out_recipient_excluded_from_catch_all(
    api_client: NetClient,
    gf_client: NetClient,
    opted_out_recipient: tuple[str, str],
    test_alert: str,
) -> None:
    """Recipient with auto_subscribe=False must not appear in catch-all routes.

    If this test fails, create_alert is not rebuilding the notification policy
    after rule creation, so Grafana routes the alert to everyone by default.
    """
    _uid, cp_name = opted_out_recipient
    catch_all = _catch_all_receiver_names(gf_client)
    assert cp_name not in catch_all, (
        f"Recipient {cp_name!r} (auto_subscribe=False) is in catch-all routes "
        f"after a new alert was created. "
        f"Catch-all currently contains: {catch_all}"
    )
