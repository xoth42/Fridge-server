#!/usr/bin/env python3
"""API-level test for the noDataState toggle feature.

Creates a test alert via the alert-api, verifies the noDataState value is
stored correctly in Grafana, then exercises the PATCH endpoint to toggle it,
verifying each transition against Grafana directly.

Usage:
  source .env
  python3 testui/test_no_data_state.py

Exits 0 on success, 1 on test failure, 2 on setup/config error.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv


# ─── HTTP client ──────────────────────────────────────────────────────────────


class _Client:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 20) -> None:
        self.base = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._timeout = timeout

    def _req(self, method: str, path: str, payload: object = None, extra: dict | None = None) -> object:
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

    def get(self, path: str) -> object:
        return self._req("GET", path)

    def post(self, path: str, payload: object = None, extra: dict | None = None) -> object:
        return self._req("POST", path, payload, extra)

    def patch(self, path: str, payload: object = None) -> object:
        return self._req("PATCH", path, payload)

    def delete(self, path: str) -> object:
        return self._req("DELETE", path)


# ─── Logging ──────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr, flush=True)


def _ok(msg: str) -> None:
    print(f"  [ OK ] {msg}", flush=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _get_grafana_no_data_state(gf: _Client, uid: str) -> str:
    rule = gf.get(f"/api/v1/provisioning/alert-rules/{uid}")
    return str(rule.get("noDataState", "<missing>"))  # type: ignore[union-attr]


def _get_api_no_data_state(api: _Client, uid: str) -> str:
    alerts = api.get("/alerts")
    if isinstance(alerts, list):
        for a in alerts:
            if a.get("uid") == uid:
                return str(a.get("no_data_state", "<missing>"))
    return "<not found>"


# ─── Test cases ───────────────────────────────────────────────────────────────


def _test_create_with_ok(api: _Client, gf: _Client, fridge: str, metric: str) -> str:
    """Create an alert with no_data_state='OK' (default) and verify."""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"[TEST] no-data-ok-{ts}"
    _log(f"Creating alert '{name}' with no_data_state='OK'...")
    result = api.post("/alerts", {
        "name": name,
        "fridge": fridge,
        "metric": metric,
        "operator": ">",
        "threshold": 9999.0,
        "no_data_state": "OK",
    })
    uid = result.get("uid")  # type: ignore[union-attr]
    if not uid:
        raise RuntimeError(f"No uid in create response: {result}")

    gf_state = _get_grafana_no_data_state(gf, uid)
    if gf_state != "OK":
        raise AssertionError(f"Grafana noDataState={gf_state!r}, expected 'OK'")
    api_state = _get_api_no_data_state(api, uid)
    if api_state != "OK":
        raise AssertionError(f"Alert-api no_data_state={api_state!r}, expected 'OK'")
    _ok(f"Created with noDataState=OK (uid={uid})")
    return str(uid)


def _test_create_with_alerting(api: _Client, gf: _Client, fridge: str, metric: str) -> str:
    """Create an alert with no_data_state='Alerting' and verify."""
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"[TEST] no-data-alerting-{ts}"
    _log(f"Creating alert '{name}' with no_data_state='Alerting'...")
    result = api.post("/alerts", {
        "name": name,
        "fridge": fridge,
        "metric": metric,
        "operator": ">",
        "threshold": 9999.0,
        "no_data_state": "Alerting",
    })
    uid = result.get("uid")  # type: ignore[union-attr]
    if not uid:
        raise RuntimeError(f"No uid in create response: {result}")

    gf_state = _get_grafana_no_data_state(gf, uid)
    if gf_state != "Alerting":
        raise AssertionError(f"Grafana noDataState={gf_state!r}, expected 'Alerting'")
    api_state = _get_api_no_data_state(api, uid)
    if api_state != "Alerting":
        raise AssertionError(f"Alert-api no_data_state={api_state!r}, expected 'Alerting'")
    _ok(f"Created with noDataState=Alerting (uid={uid})")
    return str(uid)


def _test_patch_toggle(api: _Client, gf: _Client, uid: str) -> None:
    """Toggle noDataState via PATCH and verify each direction."""
    _log(f"PATCH uid={uid}: OK → Alerting...")
    api.patch(f"/alerts/{uid}/no-data-state", {"no_data_state": "Alerting"})
    gf_state = _get_grafana_no_data_state(gf, uid)
    if gf_state != "Alerting":
        raise AssertionError(f"After PATCH to Alerting, Grafana has noDataState={gf_state!r}")
    api_state = _get_api_no_data_state(api, uid)
    if api_state != "Alerting":
        raise AssertionError(f"After PATCH to Alerting, alert-api has no_data_state={api_state!r}")
    _ok("PATCH OK → Alerting confirmed in Grafana and alert-api")

    _log(f"PATCH uid={uid}: Alerting → OK...")
    api.patch(f"/alerts/{uid}/no-data-state", {"no_data_state": "OK"})
    gf_state = _get_grafana_no_data_state(gf, uid)
    if gf_state != "OK":
        raise AssertionError(f"After PATCH to OK, Grafana has noDataState={gf_state!r}")
    api_state = _get_api_no_data_state(api, uid)
    if api_state != "OK":
        raise AssertionError(f"After PATCH to OK, alert-api has no_data_state={api_state!r}")
    _ok("PATCH Alerting → OK confirmed in Grafana and alert-api")


def _test_invalid_value_rejected(api: _Client) -> None:
    """Verify the API rejects invalid no_data_state values."""
    _log("Checking that invalid no_data_state is rejected by create endpoint...")
    try:
        api.post("/alerts", {
            "name": "[TEST] should-fail",
            "fridge": "fridge-test",
            "metric": "ch1_t_kelvin",
            "operator": ">",
            "threshold": 1.0,
            "no_data_state": "BadValue",
        })
        raise AssertionError("Expected HTTP 422 for invalid no_data_state, got success")
    except RuntimeError as exc:
        if "422" not in str(exc) and "400" not in str(exc):
            raise AssertionError(f"Expected 422/400, got: {exc}") from exc
    _ok("Invalid no_data_state correctly rejected (422)")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Test noDataState create/patch feature")
    parser.add_argument("--api-url", default=os.getenv("ALERT_API_URL", "http://localhost:8000/api"))
    parser.add_argument("--grafana-url", default=os.getenv("GRAFANA_URL", "http://localhost:3000"))
    parser.add_argument("-u", "--username", default=os.getenv("GF_ADMIN_USER"))
    parser.add_argument("-p", "--password", default=os.getenv("GF_ADMIN_PASSWORD"))
    parser.add_argument("--fridge", default="fridge-dodo", help="Fridge ID to use for test alerts")
    parser.add_argument("--metric", default="ch1_t_kelvin", help="Metric to use for test alerts")
    args = parser.parse_args()

    if not args.username or not args.password:
        print("Missing credentials. Set GF_ADMIN_USER / GF_ADMIN_PASSWORD.", file=sys.stderr)
        return 2

    gf = _Client(args.grafana_url, args.username, args.password)
    api = _Client(args.api_url, args.username, args.password)

    try:
        gf.get("/api/health")
    except RuntimeError as exc:
        print(f"Grafana unreachable: {exc}", file=sys.stderr)
        return 2
    try:
        api.get("/health")
    except RuntimeError as exc:
        print(f"Alert-api unreachable: {exc}", file=sys.stderr)
        return 2

    print("\n── noDataState Toggle Test ──")
    print(f"  Grafana  : {args.grafana_url}")
    print(f"  Alert-api: {args.api_url}")
    print()

    created_uids: list[str] = []
    failures: list[str] = []

    tests = [
        ("create with OK",      lambda: created_uids.append(_test_create_with_ok(api, gf, args.fridge, args.metric))),
        ("create with Alerting", lambda: created_uids.append(_test_create_with_alerting(api, gf, args.fridge, args.metric))),
        ("patch toggle",        lambda: _test_patch_toggle(api, gf, created_uids[0]) if created_uids else None),
        ("invalid value rejected", lambda: _test_invalid_value_rejected(api)),
    ]

    for name, fn in tests:
        try:
            fn()
        except (AssertionError, RuntimeError) as exc:
            _fail(f"{name}: {exc}")
            failures.append(name)

    print("\n  Cleaning up test alerts...")
    for uid in created_uids:
        try:
            api.delete(f"/alerts/{uid}")
            _log(f"Deleted test alert {uid}")
        except Exception as exc:
            print(f"  [WARN] Could not delete {uid}: {exc}", file=sys.stderr)

    print()
    if failures:
        _fail(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        return 1

    _ok(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
