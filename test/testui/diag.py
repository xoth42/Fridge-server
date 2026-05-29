#!/usr/bin/env python3
"""
Grafana email routing diagnostic for the fridge alert system.

Inspects live Grafana state, simulates the expected policy (replicating the
rebuild_notification_policy logic from grafana_client.py), diffs the two, and
shows which receivers each alert would actually reach.

Usage (quickest):
  export GF_ADMIN_USER=admin GF_ADMIN_PASSWORD=secret
  python3 testui/diag.py

Full options:
  python3 testui/diag.py \\
    --grafana-url http://localhost:3000 \\
    -u admin -p secret \\
    [--api-url http://localhost:8000/api] \\
    [--rebuild]            # push corrected policy straight to Grafana
    [--test-send]          # trigger one-shot email via alert-api (admin required)
    [--fire-cycle TITLE]   # disable/re-enable alert to reset notification state
    [--verbose]            # print full policy JSON

Exit codes:
  0  no issues found
  1  issues detected (see Issues section)
  2  connection / auth error
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import textwrap
import time
from urllib import error, request
from urllib.parse import urlencode


# Receivers expected to exist as provisioned contact points but not created
# through the alert-api.  Referenced by name in the rebuilt policy.
# Reads from the same env vars used by the alert-api container.
PROVISIONED_RECEIVERS = {
    os.environ.get("GRAFANA_RECEIVER_SLACK", "lab-slack"),
    os.environ.get("GRAFANA_RECEIVER_EMAIL", "lab-email"),
}
_PLACEHOLDER_DOMAINS = ("@example.com",)


def _split_addresses(raw: str) -> list[str]:
    """Normalise a Grafana addresses field to a list of cleaned email strings."""
    if not raw:
        return []
    return [
        p.strip().strip("<>")
        for p in re.split(r"[;,\s]+", raw)
        if p.strip() and "@" in p
    ]


def _cp_has_real_addresses(cp: dict) -> bool:
    """True if the contact point has at least one non-placeholder address."""
    addrs = _split_addresses((cp.get("settings") or {}).get("addresses", ""))
    return any(
        not any(a.lower().endswith(d) for d in _PLACEHOLDER_DOMAINS)
        for a in addrs
    )


# ─────────────────────────────── HTTP clients ────────────────────────────────


class HttpClient:
    """Basic-auth HTTP client backed by stdlib urllib."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers: dict[str, str] = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: object = None,
        extra_headers: dict[str, str] | None = None,
    ) -> object:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {**self._headers, **(extra_headers or {})}
        req = request.Request(url=url, method=method, headers=headers, data=data)
        try:
            with request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode()
                return json.loads(body) if body.strip() else {}
        except error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} → HTTP {exc.code}: {body[:500]}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"{method} {path} → {exc}") from exc

    def get(self, path: str, params: dict | None = None) -> object:
        if params:
            path = f"{path}?{urlencode(params)}"
        return self._request("GET", path)

    def put(self, path: str, body: object, extra_headers: dict | None = None) -> object:
        return self._request("PUT", path, body, extra_headers)

    def post(self, path: str, body: object) -> object:
        return self._request("POST", path, body)

    def patch(self, path: str, body: object) -> object:
        return self._request("PATCH", path, body)

    def delete(self, path: str) -> object:
        return self._request("DELETE", path)




# ───────────────────────────── State fetching ────────────────────────────────


def fetch_state(gf: GrafanaApi) -> tuple[list, dict, list, dict]:
    """Return (contact_points, live_policy, alert_rules, auto_settings)."""
    contact_points = gf.get("/api/v1/provisioning/contact-points")
    assert isinstance(contact_points, list)

    try:
        live_policy = gf.get("/api/v1/provisioning/policies")
        assert isinstance(live_policy, dict)
    except (RuntimeError, AssertionError) as exc:
        _warn(f"Could not fetch policy: {exc}")
        live_policy = {}

    alert_rules = gf.get("/api/v1/provisioning/alert-rules")
    assert isinstance(alert_rules, list)

    # Enrich rules with live state from the Prometheus-compatible endpoint.
    try:
        state_data = gf.get("/api/prometheus/grafana/api/v1/rules")
        assert isinstance(state_data, dict)
        state_map: dict[str, str] = {}
        for group in state_data.get("data", {}).get("groups", []):
            for rule in group.get("rules", []):
                raw = rule.get("state", "unknown")
                state_map[rule.get("name", "")] = "normal" if raw == "inactive" else raw
        for rule in alert_rules:
            rule["_state"] = state_map.get(rule.get("title", ""), "unknown")
    except Exception:
        pass

    # Auto-subscribe settings from Grafana annotation store.
    auto_settings: dict[str, bool] = {}
    try:
        ann_resp = gf.get("/api/annotations", params={"tags": "recipient-auto-subscribe", "limit": "1"})
        assert isinstance(ann_resp, list)
        if ann_resp:
            raw_text = ann_resp[0].get("text", "{}")
            auto_settings = json.loads(raw_text)
    except Exception:
        pass

    return contact_points, live_policy, alert_rules, auto_settings


# ──────────────────────────── Policy simulation ──────────────────────────────


def simulate_policy(
    contact_points: list[dict],
    alert_rules: list[dict],
    auto_settings: dict[str, bool],
) -> dict:
    """Replicate rebuild_notification_policy from grafana_client.py exactly."""
    uid_to_name = {cp.get("uid", ""): cp.get("name", "") for cp in contact_points}

    # Auto-subscribed email contact points with real addresses (default True for unknown UIDs).
    # Skips CPs with blank UIDs or only placeholder addresses — same filter as
    # rebuild_notification_policy in grafana_client.py.
    auto_email_names: list[str] = [
        cp["name"]
        for cp in contact_points
        if cp.get("type") == "email"
        and cp.get("name")
        and cp.get("uid")
        and _cp_has_real_addresses(cp)
        and auto_settings.get(cp.get("uid", ""), True)
    ]

    # Collect UIDs that are explicitly referenced in alert notify_to labels.
    active_uids: set[str] = set()
    for rule in alert_rules:
        raw = rule.get("labels", {}).get("notify_to", "")
        for uid in raw.split(","):
            uid = uid.strip()
            if uid:
                active_uids.add(uid)

    # Per-recipient routes (sorted for determinism).
    per_recipient: list[dict] = []
    for uid in sorted(active_uids):
        name = uid_to_name.get(uid)
        if name:
            per_recipient.append(
                {
                    "receiver": name,
                    "continue": True,
                    "object_matchers": [["notify_to", "=~", f".*{uid}.*"]],
                }
            )

    # Catch-all routes — guarded when per-recipient routes exist so that
    # explicitly-routed alerts don't also hit the catch-all.
    guarded = bool(per_recipient)
    catch_all: list[dict] = [
        {
            "receiver": os.environ.get("GRAFANA_RECEIVER_SLACK", "lab-slack"),
            "continue": True,
            **({"object_matchers": [["notify_to", "!~", ".+"]]} if guarded else {}),
        }
    ]
    for name in auto_email_names:
        route: dict = {"receiver": name, "continue": True}
        if guarded:
            route["object_matchers"] = [["notify_to", "!~", ".+"]]
        catch_all.append(route)

    return {
        "receiver": os.environ.get("GRAFANA_RECEIVER_EMAIL", "lab-email"),
        "group_by": [],
        "group_wait": "10s",
        "group_interval": "2m",
        "repeat_interval": "4h",
        "routes": per_recipient + catch_all,
    }


# ─────────────────────────── Policy drift detection ──────────────────────────


def _route_key(route: dict) -> tuple:
    """Canonical key for a route (receiver + sorted matchers)."""
    matchers = tuple(sorted(tuple(m) for m in route.get("object_matchers", [])))
    return (route.get("receiver", ""), matchers)


def compare_policies(live: dict, expected: dict) -> list[str]:
    """Return human-readable list of differences between live and expected policy."""
    diffs: list[str] = []

    # Root-level timing fields (group_by excluded — Grafana may add defaults).
    for field in ("receiver", "group_wait", "group_interval", "repeat_interval"):
        lv = live.get(field)
        ev = expected.get(field)
        if lv != ev:
            diffs.append(f"root.{field}: live={lv!r}  expected={ev!r}")

    live_routes = {_route_key(r): r for r in live.get("routes", [])}
    exp_routes = {_route_key(r): r for r in expected.get("routes", [])}

    for key, route in exp_routes.items():
        if key not in live_routes:
            diffs.append(
                f"Missing route: receiver={route.get('receiver')!r}  "
                f"matchers={route.get('object_matchers')}"
            )

    for key, route in live_routes.items():
        if key not in exp_routes:
            diffs.append(
                f"Extra route:   receiver={route.get('receiver')!r}  "
                f"matchers={route.get('object_matchers')}"
            )

    return diffs


# ─────────────────────────── Route coverage ──────────────────────────────────


def which_receivers_fire(rule: dict, policy: dict) -> list[str]:
    """Return ordered list of receiver names that would fire for this alert rule."""
    label_set = dict(rule.get("labels", {}))
    fired: list[str] = []

    for route in policy.get("routes", []):
        matchers = route.get("object_matchers", [])
        all_match = True
        for m in matchers:
            label, op, value = m[0], m[1], m[2]
            label_val = label_set.get(label, "")
            if op == "=~":
                if not re.search(value, label_val):
                    all_match = False
                    break
            elif op == "!~":
                if re.search(value, label_val):
                    all_match = False
                    break
            elif op == "=":
                if label_val != value:
                    all_match = False
                    break
            elif op == "!=":
                if label_val == value:
                    all_match = False
                    break

        if all_match:
            fired.append(route.get("receiver", "?"))
            if not route.get("continue", False):
                break

    return fired


# ──────────────────────────── Output helpers ─────────────────────────────────


def _hr(title: str = "") -> None:
    bar = "─" * 60
    if title:
        print(f"\n{bar}")
        print(f"  {title}")
        print(bar)
    else:
        print(bar)


def _warn(msg: str) -> None:
    print(f"  WARNING: {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"  {msg} ✓")


# ──────────────────────────── Section printers ───────────────────────────────


def section_contact_points(
    contact_points: list[dict],
    auto_settings: dict[str, bool],
) -> list[str]:
    issues: list[str] = []
    _hr("1 / 6  Contact Points")

    email_cps = [cp for cp in contact_points if cp.get("type") == "email"]
    other_cps = [cp for cp in contact_points if cp.get("type") != "email"]
    cp_names = {cp.get("name", "") for cp in contact_points}

    print(f"\n  Email contact points ({len(email_cps)}):")
    print(f"  {'Name':<32} {'UID':<24} {'Address(es)':<30} {'single?'}")
    print(f"  {'-'*32} {'-'*24} {'-'*30} {'-'*7}")

    for cp in email_cps:
        uid = cp.get("uid", "")
        name = cp.get("name", "")
        settings = cp.get("settings") or {}
        raw_addr = settings.get("addresses", "")
        single = settings.get("singleEmail", False)

        addr_parts = _split_addresses(raw_addr)
        placeholders = [a for a in addr_parts if any(a.lower().endswith(d) for d in _PLACEHOLDER_DOMAINS)]
        real_addrs = [a for a in addr_parts if a not in placeholders]

        flags: list[str] = []
        if single:
            flags.append("singleEmail=True")
            issues.append(
                f"Contact point {name!r} (uid={uid}) has singleEmail=True — "
                "recipients share one grouped email instead of individual ones. "
                "Run: POST /api/recipients/sync-email-format  to fix."
            )
        if placeholders and not real_addrs:
            flags.append("PLACEHOLDER ONLY")
            issues.append(
                f"Contact point {name!r} has only placeholder address(es): {placeholders}. "
                "No real emails will be delivered through this contact point."
            )

        addr_display = ", ".join(real_addrs) if real_addrs else (", ".join(placeholders) or "(empty)")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        single_str = "YES !" if single else "no"
        print(f"  {name:<32} {uid:<24} {addr_display:<30} {single_str}{flag_str}")

    if other_cps:
        print(f"\n  Other contact points ({len(other_cps)}):")
        for cp in other_cps:
            print(f"  {cp.get('name', ''):<32} type={cp.get('type', '')}")

    # Check that hardcoded provisioned receivers exist.
    for recv in sorted(PROVISIONED_RECEIVERS):
        if recv not in cp_names:
            issues.append(
                f"Expected receiver {recv!r} not found in any contact point — "
                "policy routes that reference it will silently fail."
            )
            print(f"\n  ! Missing provisioned receiver: {recv}")

    if not issues:
        _ok("Contact points look clean")

    return issues


def section_auto_subscribe(
    contact_points: list[dict],
    auto_settings: dict[str, bool],
) -> None:
    _hr("2 / 6  Auto-Subscribe Settings")
    email_cps = [cp for cp in contact_points if cp.get("type") == "email"]

    if not auto_settings:
        print("  (no annotation found — all recipients default to auto-subscribed)")

    print(f"\n  {'Name':<32} {'UID':<24} Status")
    print(f"  {'-'*32} {'-'*24} ------")
    for cp in email_cps:
        uid = cp.get("uid", "")
        name = cp.get("name", "")
        subscribed = auto_settings.get(uid, True)
        status = "auto-subscribed" if subscribed else "opted out (catch-all excluded)"
        print(f"  {name:<32} {uid:<24} {status}")


def section_alert_rules(
    alert_rules: list[dict],
    contact_points: list[dict],
    live_policy: dict,
) -> list[str]:
    issues: list[str] = []
    _hr("3 / 6  Alert Rules → Route Coverage")

    uid_to_name = {cp.get("uid", ""): cp.get("name", "") for cp in contact_points}

    if not alert_rules:
        print("  (no alert rules found)")
        return issues

    print(
        f"\n  {'Title':<36} {'State':<9} {'notify_to':<18} Receivers that fire"
    )
    print(f"  {'-'*36} {'-'*9} {'-'*18} {'-'*35}")

    for rule in alert_rules:
        title = rule.get("title", "")
        uid = rule.get("uid", "")
        state = rule.get("_state", "?")
        raw_notify = rule.get("labels", {}).get("notify_to", "")
        notify_uids = [u.strip() for u in raw_notify.split(",") if u.strip()]

        # Validate that all referenced UIDs are known contact points.
        for nuid in notify_uids:
            if nuid not in uid_to_name:
                issues.append(
                    f"Alert {title!r}: notify_to references unknown contact UID {nuid!r}. "
                    "This route will be silently ignored."
                )

        if notify_uids:
            names = [uid_to_name.get(u, f"?{u[:8]}") for u in notify_uids]
            routing_label = f"explicit({len(notify_uids)})"
        else:
            routing_label = "catch-all"

        fired = which_receivers_fire(rule, live_policy)
        fired_str = ", ".join(fired) if fired else "NONE !"

        if not fired:
            issues.append(
                f"Alert {title!r} (state={state}) matches NO live policy routes — "
                "notifications will not be delivered."
            )

        print(f"  {title:<36} {state:<9} {routing_label:<18} {fired_str}")

    if not issues:
        _ok("All alerts have at least one matching route")

    return issues


def section_live_policy(live_policy: dict, contact_points: list[dict], verbose: bool) -> list[str]:
    issues: list[str] = []
    _hr("4 / 6  Live Notification Policy")

    cp_names = {cp.get("name", "") for cp in contact_points} | PROVISIONED_RECEIVERS
    root_recv = live_policy.get("receiver", "(none)")

    if not live_policy:
        issues.append("No live notification policy found — notifications cannot be routed.")
        print("  (no policy returned by Grafana)")
        return issues

    print(
        f"\n  Root receiver : {root_recv}"
        + ("  !" if root_recv not in cp_names else "")
    )
    print(f"  group_wait    : {live_policy.get('group_wait')}")
    print(f"  group_interval: {live_policy.get('group_interval')}")
    print(f"  repeat_interval: {live_policy.get('repeat_interval')}")

    routes = live_policy.get("routes", [])
    print(f"\n  Routes ({len(routes)}):")
    print(f"  {'#':<4} {'Receiver':<30} {'Matchers':<42} continue")
    print(f"  {'─'*4} {'─'*30} {'─'*42} {'─'*8}")

    for i, route in enumerate(routes):
        recv = route.get("receiver", "?")
        cont = route.get("continue", False)
        matchers = route.get("object_matchers", [])
        matcher_str = " AND ".join(f"{m[0]}{m[1]}{m[2]!r}" for m in matchers) if matchers else "(catch-all)"
        unknown = " !" if recv not in cp_names else ""
        print(f"  {i:<4} {recv:<30}{unknown} {matcher_str:<42} {str(cont).lower()}")
        if recv not in cp_names:
            issues.append(
                f"Policy route #{i} references unknown receiver {recv!r}. "
                "This route exists in the policy but the contact point is missing."
            )

    if root_recv not in cp_names:
        issues.append(
            f"Policy root receiver {root_recv!r} not found in contact points."
        )

    if verbose:
        print("\n  Full JSON:")
        print(textwrap.indent(json.dumps(live_policy, indent=2), "    "))

    return issues


def section_expected_policy(
    expected_policy: dict,
    verbose: bool,
) -> None:
    _hr("5 / 6  Expected Policy  (simulated rebuild)")
    routes = expected_policy.get("routes", [])
    print(f"\n  Root receiver : {expected_policy.get('receiver')}")
    print(f"  repeat_interval: {expected_policy.get('repeat_interval')}  "
          f"group_wait: {expected_policy.get('group_wait')}  "
          f"group_interval: {expected_policy.get('group_interval')}")
    print(f"\n  Routes ({len(routes)}):")
    for i, route in enumerate(routes):
        recv = route.get("receiver", "?")
        matchers = route.get("object_matchers", [])
        matcher_str = " AND ".join(f"{m[0]}{m[1]}{m[2]!r}" for m in matchers) if matchers else "(catch-all)"
        print(f"  {i:<4} {recv:<30} {matcher_str}")

    if verbose:
        print("\n  Full JSON:")
        print(textwrap.indent(json.dumps(expected_policy, indent=2), "    "))


def section_drift(live_policy: dict, expected_policy: dict) -> list[str]:
    _hr("6 / 6  Policy Drift  (live vs expected)")
    diffs = compare_policies(live_policy, expected_policy)
    if not diffs:
        _ok("Live policy matches expected rebuild output")
    else:
        print(f"\n  {len(diffs)} difference(s) found:")
        for d in diffs:
            print(f"  ! {d}")
        print(
            "\n  Fix: run  python3 testui/diag.py --rebuild  "
            "to push the expected policy to Grafana."
        )
    return [f"DRIFT: {d}" for d in diffs]


# ──────────────────────────── Actions ────────────────────────────────────────


def action_rebuild(gf: GrafanaApi, expected_policy: dict) -> None:
    _hr("Action: Rebuild Policy")
    print("  Pushing expected policy to Grafana...")
    try:
        gf.put(
            "/api/v1/provisioning/policies",
            expected_policy,
            extra_headers={"X-Disable-Provenance": "true"},
        )
        _ok("Policy updated")
    except RuntimeError as exc:
        if "invalidProvenance" in str(exc):
            print("  Policy is file-provisioned; resetting first...")
            try:
                gf.delete("/api/v1/provisioning/policies")
                gf.put(
                    "/api/v1/provisioning/policies",
                    expected_policy,
                    extra_headers={"X-Disable-Provenance": "true"},
                )
                _ok("Policy reset and updated")
            except RuntimeError as exc2:
                print(f"  FAILED: {exc2}")
        else:
            print(f"  FAILED: {exc}")


def action_test_send(api: AlertApi) -> None:
    _hr("Action: One-Shot Test Email Send")
    try:
        result = api.post("/recipients/check", {})
        assert isinstance(result, dict)
        print(f"  sent          : {result.get('sent')}")
        print(f"  alert_name    : {result.get('alert_name')}")
        print(f"  recipient_count: {result.get('recipient_count')}")
        print(f"  addresses     : {result.get('addresses')}")
        if result.get("sent"):
            print(
                f"\n  Now check inbox with:\n"
                f"  python3 testui/check_sender_inbox.py "
                f"--query {result.get('alert_name')!r} --since-minutes 5"
            )
    except RuntimeError as exc:
        print(f"  FAILED: {exc}")


def action_fire_cycle(api: AlertApi, alert_title: str) -> None:
    _hr(f"Action: Fire Cycle for {alert_title!r}")
    try:
        alerts = api.get("/alerts")
        assert isinstance(alerts, list)
    except RuntimeError as exc:
        print(f"  Could not fetch alerts: {exc}")
        return

    alert = None
    target = alert_title.strip().lower()
    for a in alerts:
        if a.get("title", "").strip().lower() == target:
            alert = a
            break
    if alert is None:
        for a in alerts:
            if target in a.get("title", "").strip().lower():
                alert = a
                break

    if alert is None:
        print(f"  Alert {alert_title!r} not found. Available titles:")
        for a in alerts:
            print(f"    {a.get('title')}")
        return

    uid = alert.get("uid", "")
    state = alert.get("state", "?")
    enabled = alert.get("enabled", True)
    print(f"  Found: uid={uid}  state={state}  enabled={enabled}")

    if not enabled:
        print("  Alert is already disabled — re-enabling only.")
        try:
            api.patch(f"/alerts/{uid}/enabled", {"enabled": True})
            _ok("Re-enabled")
        except RuntimeError as exc:
            print(f"  Re-enable failed: {exc}")
        return

    print("  Disabling alert...")
    try:
        api.patch(f"/alerts/{uid}/enabled", {"enabled": False})
    except RuntimeError as exc:
        print(f"  Disable failed: {exc}")
        return

    print("  Waiting 3 s...")
    time.sleep(3)

    print("  Re-enabling alert...")
    try:
        api.patch(f"/alerts/{uid}/enabled", {"enabled": True})
    except RuntimeError as exc:
        print(f"  Re-enable failed: {exc}")
        return

    _ok("Alert notification state reset")
    print(
        f"\n  Wait ~30 s then check inbox with:\n"
        f"  python3 testui/check_sender_inbox.py "
        f"--query {alert_title!r} --since-minutes 5"
    )


# ──────────────────────────────── main ───────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grafana email routing diagnostic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--grafana-url",
        default=os.getenv("GRAFANA_URL", "http://localhost:3000"),
        help="Grafana base URL (default: GRAFANA_URL env or http://localhost:3000)",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("ALERT_API_URL", "http://localhost:8000/api"),
        help="Alert-api base URL (default: ALERT_API_URL env or http://localhost:8000/api)",
    )
    parser.add_argument("-u", "--username", default=os.getenv("GF_ADMIN_USER"))
    parser.add_argument("-p", "--password", default=os.getenv("GF_ADMIN_PASSWORD"))
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Push expected policy directly to Grafana (no round-trip through alert-api)",
    )
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="Send one-shot test email to all recipients via alert-api (requires admin)",
    )
    parser.add_argument(
        "--fire-cycle",
        metavar="TITLE",
        help="Disable then re-enable named alert to reset its notification state",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print full policy JSON in sections 4 and 5",
    )
    args = parser.parse_args()

    if not args.username or not args.password:
        print("Missing credentials. Use -u/-p or GF_ADMIN_USER / GF_ADMIN_PASSWORD.")
        return 2

    gf = HttpClient(args.grafana_url, args.username, args.password)
    api = HttpClient(args.api_url, args.username, args.password, timeout=20)

    print(f"Grafana : {args.grafana_url}")
    print(f"Alert-api: {args.api_url}")

    # Connectivity check.
    try:
        gf.get("/api/health")
        _ok("Grafana reachable")
    except RuntimeError as exc:
        print(f"Grafana UNREACHABLE: {exc}")
        return 2

    # Fetch all state.
    print("\nFetching Grafana state...")
    try:
        contact_points, live_policy, alert_rules, auto_settings = fetch_state(gf)
    except (RuntimeError, AssertionError) as exc:
        print(f"Failed to fetch state: {exc}")
        return 2
    print(
        f"  {len(contact_points)} contact point(s)  |  "
        f"{len(alert_rules)} alert rule(s)  |  "
        f"{len(auto_settings)} auto-subscribe override(s)"
    )

    all_issues: list[str] = []

    # Run diagnostic sections.
    all_issues += section_contact_points(contact_points, auto_settings)
    section_auto_subscribe(contact_points, auto_settings)
    all_issues += section_alert_rules(alert_rules, contact_points, live_policy)
    all_issues += section_live_policy(live_policy, contact_points, args.verbose)

    expected_policy = simulate_policy(contact_points, alert_rules, auto_settings)
    section_expected_policy(expected_policy, args.verbose)
    all_issues += section_drift(live_policy, expected_policy)

    # Issues summary.
    _hr("Issues Summary")
    if not all_issues:
        _ok("No issues found")
    else:
        print(f"\n  {len(all_issues)} issue(s) detected:\n")
        for i, issue in enumerate(all_issues, 1):
            # Wrap long lines.
            wrapped = textwrap.fill(issue, width=76, subsequent_indent="       ")
            print(f"  [{i:2d}] {wrapped}")

    # Optional actions.
    if args.rebuild:
        action_rebuild(gf, expected_policy)

    if args.test_send:
        action_test_send(api)

    if args.fire_cycle:
        action_fire_cycle(api, args.fire_cycle)

    return 1 if all_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
