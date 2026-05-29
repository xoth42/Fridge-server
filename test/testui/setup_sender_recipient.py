#!/usr/bin/env python3
"""Ensure alerts.wanglab@gmail.com is configured as a recipient for a target alert.

This script uses the alert-api endpoints:
- GET  /api/recipients
- POST /api/recipients
- PATCH /api/recipients/{uid}/auto-subscribe
- GET  /api/alerts
- PATCH /api/alerts/{uid}/recipients

By default it targets alert title "Dodo test 9".
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any
from urllib import error, request


class ApiClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._auth_header = f"Basic {token}"

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url=url, method=method, headers=headers, data=data)
        try:
            with request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed: {exc.code} {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload)

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PATCH", path, payload)


def pick_alert(alerts: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    target = title.strip().lower()
    for alert in alerts:
        if str(alert.get("title", "")).strip().lower() == target:
            return alert
    for alert in alerts:
        if target in str(alert.get("title", "")).strip().lower():
            return alert
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure sender recipient + assignment")
    parser.add_argument("--api-url", default=os.getenv("ALERT_API_URL", "http://localhost:8000/api"))
    parser.add_argument("--username", default=os.getenv("GF_ADMIN_USER"))
    parser.add_argument("--password", default=os.getenv("GF_ADMIN_PASSWORD"))
    parser.add_argument("--recipient-name", default="alerts.wanglab@gmail.com")
    parser.add_argument("--recipient-email", default="alerts.wanglab@gmail.com")
    parser.add_argument("--alert-title", default="Dodo test 9")
    args = parser.parse_args()

    if not args.username or not args.password:
        print("Missing credentials. Provide --username/--password or GF_ADMIN_USER/GF_ADMIN_PASSWORD.")
        return 2

    api = ApiClient(args.api_url, args.username, args.password)

    recipients = api.get("/recipients")
    recipient = next((r for r in recipients if r.get("name") == args.recipient_name), None)

    if recipient is None:
        print(f"Creating recipient '{args.recipient_name}' -> {args.recipient_email}")
        recipient = api.post(
            "/recipients",
            {"name": args.recipient_name, "email": args.recipient_email},
        )
    else:
        print(f"Recipient already exists: {recipient.get('name')} ({recipient.get('uid')})")

    recipient_uid = recipient.get("uid")
    if not recipient_uid:
        print("Recipient UID missing in API response.")
        return 1

    # Best effort: ensure recipient is auto-subscribed.
    try:
        api.patch(f"/recipients/{recipient_uid}/auto-subscribe", {"auto_subscribe": True})
        print("Auto-subscribe enabled for recipient.")
    except RuntimeError as exc:
        print(f"Warning: could not set auto-subscribe: {exc}")

    alerts = api.get("/alerts")
    alert = pick_alert(alerts, args.alert_title)
    if alert is None:
        print(f"Alert not found for title: {args.alert_title}")
        return 1

    alert_uid = alert.get("uid")
    notify_to = list(alert.get("notify_to", []))
    print(f"Target alert: {alert.get('title')} ({alert_uid})")

    if not alert_uid:
        print("Alert UID missing in API response.")
        return 1

    if len(notify_to) == 0:
        print("Alert notify_to is empty (send to all recipients). No assignment change needed.")
        return 0

    if recipient_uid in notify_to:
        print("Recipient already assigned in notify_to list.")
        return 0

    updated = notify_to + [recipient_uid]
    api.patch(f"/alerts/{alert_uid}/recipients", {"contact_uids": updated})
    print("Recipient added to alert notify_to list.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
