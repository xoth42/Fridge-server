"""Thin async wrapper around the Grafana HTTP API used by alert-api."""

import json
import re
from datetime import datetime, timezone
import httpx

# Prometheus datasource UID — hardcoded, defined in provisioning/datasources/prometheus.yml
PROMETHEUS_DS_UID = "P1809F7CD0C75ACF3"
BASIC_SEVERITY = "warning"

# Reverse map from Grafana evaluator type back to display symbol
GRAFANA_TYPE_TO_SYMBOL: dict[str, str] = {
    "gt": ">",
    "lt": "<",
    "gte": ">=",
    "lte": "<=",
}


class GrafanaClient:
    """Wraps Grafana HTTP API calls.

    All rule-management calls use the service account token (GRAFANA_SA_TOKEN)
    so that Editor-role lab users can create/delete alerts without needing
    Admin permissions themselves.  Credential validation uses the caller's own
    Grafana username/password against /api/user.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        receiver_email: str = "lab-email",
        receiver_slack: str = "lab-slack",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {token}"}
        self.receiver_email = receiver_email
        self.receiver_slack = receiver_slack

    def _auth_kwargs(self, basic_auth: tuple[str, str] | None = None) -> dict:
        if basic_auth is not None:
            return {"auth": basic_auth}
        return {"headers": self._auth_headers}

    # ── Auth validation ───────────────────────────────────────────────────────

    async def validate_credentials(self, username: str, password: str) -> bool:
        """Return True if the Grafana username/password are valid (any role)."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            resp = await client.get("/api/user", auth=(username, password))
            return resp.status_code == 200

    async def validate_admin_credentials(self, username: str, password: str) -> bool:
        """Return True if credentials are valid and user has Grafana admin privileges."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            resp = await client.get("/api/user", auth=(username, password))
            if resp.status_code != 200:
                return False
            try:
                user = resp.json()
            except Exception:
                return False
            return bool(user.get("isGrafanaAdmin")) or str(user.get("orgRole", "")).lower() == "admin"

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """Return True if Grafana /api/health is reachable."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            try:
                resp = await client.get("/api/health")
                return resp.status_code == 200
            except httpx.RequestError:
                return False

    # ── Alert rules ───────────────────────────────────────────────────────────

    async def list_alert_rules(self) -> list[dict]:
        """Return all provisioning alert rules, enriched with current state."""
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            rules_resp = await client.get("/api/v1/provisioning/alert-rules")
            rules_resp.raise_for_status()
            rules: list[dict] = rules_resp.json()

            # Fetch current evaluation states (title → state)
            state_map: dict[str, str] = {}
            try:
                state_resp = await client.get(
                    "/api/prometheus/grafana/api/v1/rules"
                )
                if state_resp.status_code == 200:
                    for group in (
                        state_resp.json().get("data", {}).get("groups", [])
                    ):
                        for rule in group.get("rules", []):
                            raw_state = rule.get("state", "unknown")
                            # Prometheus calls "inactive" what Grafana UI calls "Normal"
                            display_state = (
                                "normal" if raw_state == "inactive" else raw_state
                            )
                            state_map[rule.get("name", "")] = display_state
            except Exception:
                pass  # state is best-effort; don't break the list endpoint

            for rule in rules:
                rule["_state"] = state_map.get(rule.get("title", ""), "unknown")

            return rules

    async def get_alert_rule(self, uid: str) -> dict:
        """Fetch a single alert rule by UID."""
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            resp = await client.get(f"/api/v1/provisioning/alert-rules/{uid}")
            resp.raise_for_status()
            return resp.json()

    async def create_alert_rule(self, rule_payload: dict) -> dict:
        """POST a new alert rule. X-Disable-Provenance keeps it UI-editable."""
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.post(
                "/api/v1/provisioning/alert-rules", json=rule_payload
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_alert_rule(self, uid: str) -> None:
        """DELETE an alert rule by UID."""
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.delete(f"/api/v1/provisioning/alert-rules/{uid}")
            resp.raise_for_status()

    # ── Disabled-rule store (Grafana annotations) ─────────────────────────────
    #
    # We use Grafana's own annotation store as a persistent KV store.
    # Each disabled rule is stored as a global annotation with:
    #   tags: ["disabled-alert-rule", "disabled-alert:<uid>"]
    #   text: full Grafana rule JSON
    #
    # On re-enable the rule is POSTed back with its original UID so nothing
    # in the UI needs to change.

    _DISABLED_TAG = "disabled-alert-rule"

    def _disabled_uid_tag(self, uid: str) -> str:
        return f"disabled-alert:{uid}"

    # ── Recipient auto-subscribe settings ─────────────────────────────────
    # Stored as a single Grafana annotation with tag "recipient-auto-subscribe"
    # whose text is a JSON object: {uid: bool, ...}.  Missing UIDs default True.

    _AUTO_SUBSCRIBE_TAG = "recipient-auto-subscribe"

    async def get_auto_subscribe_settings(self, basic_auth: tuple[str, str] | None = None) -> dict[str, bool]:
        """Return {contact_uid: auto_subscribe} for all stored settings."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=10.0, **self._auth_kwargs(basic_auth)
            ) as client:
                resp = await client.get(
                    "/api/annotations",
                    params={"tags": self._AUTO_SUBSCRIBE_TAG, "limit": 1},
                )
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    return {}
                try:
                    return json.loads(items[0]["text"])
                except Exception:
                    return {}
        except httpx.HTTPError:
            # Annotation permissions may be restricted in some Grafana setups.
            # Fallback to default behavior: all recipients are auto-subscribed.
            return {}

    @staticmethod
    def _split_email_addresses(raw: str) -> list[str]:
        """Split comma/semicolon/space separated addresses into clean parts."""
        if not raw:
            return []
        parts = re.split(r"[;,\s]+", raw)
        cleaned = [p.strip().strip("<>") for p in parts if p and "@" in p]
        return [p for p in cleaned if p]

    _PLACEHOLDER_DOMAINS = ("@example.com",)

    @classmethod
    def _cp_is_routable(cls, cp: dict) -> bool:
        """Return True only if the contact point has a UID and at least one real address.

        Skips contact points with a blank UID (broken/legacy) or whose addresses
        are all placeholder values — routing to these would silently fail or bounce.
        """
        if not cp.get("uid"):
            return False
        addresses = cls._split_email_addresses(
            (cp.get("settings") or {}).get("addresses", "")
        )
        return any(
            not any(addr.lower().endswith(d) for d in cls._PLACEHOLDER_DOMAINS)
            for addr in addresses
        )

    async def list_email_recipients(self, basic_auth: tuple[str, str] | None = None) -> list[dict]:
        """Return flattened email recipients from configured email contact points."""
        cps = await self.list_contact_points(basic_auth=basic_auth)
        recipients: list[dict] = []
        for cp in cps:
            if cp.get("type") != "email":
                continue
            cp_name = cp.get("name", "")
            cp_uid = cp.get("uid", "")
            addresses = self._split_email_addresses(cp.get("settings", {}).get("addresses", ""))
            for addr in addresses:
                addr_l = addr.lower()
                if addr_l == "example@email.com" or addr_l.endswith("@example.com"):
                    continue
                recipients.append({
                    "contact_uid": cp_uid,
                    "contact_name": cp_name,
                    "address": addr,
                })
        return recipients

    async def send_test_email_to_all_recipients(self, basic_auth: tuple[str, str] | None = None) -> dict:
        """Trigger a one-shot Grafana notification test to all configured email addresses."""
        recipients = await self.list_email_recipients(basic_auth=basic_auth)
        deduped: list[str] = []
        seen: set[str] = set()
        for item in recipients:
            addr = item["address"].lower()
            if addr not in seen:
                seen.add(addr)
                deduped.append(item["address"])

        if not deduped:
            return {
                "sent": False,
                "recipient_count": 0,
                "addresses": [],
                "message": "No email recipients configured.",
            }

        # Numeric suffix helps correlate received/missing deliveries.
        test_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        alert_name = f"TestAlert-{test_id}"

        payload = {
            "receivers": [
                {
                    "name": "recipient-check",
                    "grafana_managed_receiver_configs": [
                        {
                            "name": "recipient-check",
                            "type": "email",
                            "settings": {
                                "addresses": ",".join(deduped),
                                "singleEmail": False,
                            },
                            "disableResolveMessage": False,
                        }
                    ],
                }
            ],
            "alert": {
                "labels": {
                    "alertname": alert_name,
                    "instance": "Grafana",
                },
                "annotations": {
                    "summary": f"Notification test {test_id}",
                    "__value_string__": "[ metric='foo' labels={instance=bar} value=10 ]",
                },
            },
        }

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=15.0, **self._auth_kwargs(basic_auth)
        ) as client:
            resp = await client.post(
                "/api/alertmanager/grafana/config/api/v1/receivers/test",
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()

        return {
            "sent": True,
            "test_id": test_id,
            "alert_name": alert_name,
            "recipient_count": len(deduped),
            "addresses": deduped,
            "result": body,
        }

    async def set_auto_subscribe(
        self,
        contact_uid: str,
        enabled: bool,
        basic_auth: tuple[str, str] | None = None,
    ) -> None:
        """Persist auto_subscribe setting for one contact point."""
        settings = await self.get_auto_subscribe_settings(basic_auth=basic_auth)
        settings[contact_uid] = enabled

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=10.0, **self._auth_kwargs(basic_auth)
        ) as client:
            # Delete existing annotation first
            existing = await client.get(
                "/api/annotations",
                params={"tags": self._AUTO_SUBSCRIBE_TAG, "limit": 1},
            )
            existing.raise_for_status()
            for item in existing.json():
                await client.delete(f"/api/annotations/{item['id']}")

            # Write updated settings
            resp = await client.post("/api/annotations", json={
                "tags": [self._AUTO_SUBSCRIBE_TAG],
                "text": json.dumps(settings),
            })
            resp.raise_for_status()

    async def store_disabled_rule(self, uid: str, rule: dict) -> None:
        """Save a rule's JSON into a Grafana annotation (replaces any existing)."""
        await self._delete_disabled_annotation(uid)  # avoid duplicates
        payload = {
            "tags": [self._DISABLED_TAG, self._disabled_uid_tag(uid)],
            "text": json.dumps(rule),
        }
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            resp = await client.post("/api/annotations", json=payload)
            resp.raise_for_status()

    async def pop_disabled_rule(self, uid: str) -> dict | None:
        """Return the stored rule dict and remove the annotation, or None."""
        ann = await self._find_disabled_annotation(uid)
        if ann is None:
            return None
        ann_id, rule = ann
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            await client.delete(f"/api/annotations/{ann_id}")
        return rule

    async def list_disabled_rules(self) -> list[dict]:
        """Return all disabled rule dicts from annotations."""
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            resp = await client.get(
                "/api/annotations",
                params={"tags": self._DISABLED_TAG, "limit": 500},
            )
            resp.raise_for_status()
            rules = []
            for item in resp.json():
                try:
                    rules.append(json.loads(item["text"]))
                except Exception:
                    pass
            return rules

    async def _find_disabled_annotation(self, uid: str) -> tuple[int, dict] | None:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            resp = await client.get(
                "/api/annotations",
                params={"tags": self._disabled_uid_tag(uid), "limit": 1},
            )
            resp.raise_for_status()
            items = resp.json()
            if not items:
                return None
            return items[0]["id"], json.loads(items[0]["text"])

    async def _delete_disabled_annotation(self, uid: str) -> None:
        ann = await self._find_disabled_annotation(uid)
        if ann:
            ann_id, _ = ann
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=self._auth_headers, timeout=10.0
            ) as client:
                await client.delete(f"/api/annotations/{ann_id}")

    async def set_alert_notify_to(self, uid: str, contact_uids: list[str]) -> None:
        """Set the notify_to routing label on an alert rule."""
        rule = await self.get_alert_rule(uid)
        labels = rule.get("labels", {})
        if contact_uids:
            labels["notify_to"] = ",".join(contact_uids)
        else:
            labels.pop("notify_to", None)
        rule["labels"] = labels
        rule.pop("provenance", None)
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.put(
                f"/api/v1/provisioning/alert-rules/{uid}", json=rule
            )
            resp.raise_for_status()

    async def rebuild_notification_policy(
        self,
        alert_items: list[dict],
        basic_auth: tuple[str, str] | None = None,
        exclude_uids: set[str] | None = None,
    ) -> None:
        """Regenerate the Grafana notification policy so that:
        - Alerts with a notify_to label go only to the listed contact points.
        - Alerts without notify_to fall through to the default catch-all.
        """
        # Collect which contact UIDs are actively referenced
        exclude = exclude_uids or set()
        active_uids: set[str] = set()
        for item in alert_items:
            for uid in item.get("notify_to", []):
                if uid and uid not in exclude:
                    active_uids.add(uid)

        # Get all contact points — both for uid→name lookup and catch-all building.
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=10.0, **self._auth_kwargs(basic_auth)
        ) as client:
            cp_resp = await client.get("/api/v1/provisioning/contact-points")
            cp_resp.raise_for_status()
            all_cps: list[dict] = cp_resp.json()

        uid_to_name = {cp.get("uid", ""): cp.get("name", "") for cp in all_cps}

        # All email contact point names — used to build the catch-all so that
        # every configured recipient receives alerts with no explicit notify_to.
        all_email_names: list[str] = [
            cp["name"] for cp in all_cps
            if cp.get("type") == "email" and cp.get("name")
        ]

        # Load auto-subscribe settings; default True for unknown UIDs.
        auto_settings = await self.get_auto_subscribe_settings(basic_auth=basic_auth)
        auto_email_names: list[str] = [
            cp["name"] for cp in all_cps
            if cp.get("type") == "email" and cp.get("name")
            and self._cp_is_routable(cp)
            and auto_settings.get(cp.get("uid", ""), True)
            and cp.get("uid", "") not in exclude
        ]

        # Per-recipient routes: match notify_to label, continue so multiple
        # recipients can all fire on the same alert.
        per_recipient: list[dict] = []
        for uid in sorted(active_uids):
            name = uid_to_name.get(uid)
            if name:
                per_recipient.append({
                    "receiver": name,
                    "continue": True,
                    "object_matchers": [["notify_to", "=~", f".*{uid}.*"]],
                })

        # Catch-all routes — include ALL email contact points so every
        # recipient gets alerts that have no explicit notify_to assignment.
        # When per_recipient is non-empty, guard with notify_to!~'.+' so
        # explicitly-assigned alerts don't also hit the catch-all.
        guarded = bool(per_recipient)
        catch_all: list[dict] = [
            {
                "receiver": self.receiver_slack,
                "continue": True,
                **({"object_matchers": [["notify_to", "!~", ".+"]]} if guarded else {}),
            }
        ]
        for name in auto_email_names:
            route: dict = {"receiver": name, "continue": True}
            if guarded:
                route["object_matchers"] = [["notify_to", "!~", ".+"]]
            catch_all.append(route)

        policy = {
            "receiver": self.receiver_email,
            "group_by": [],
            "group_wait": "10s",
            "group_interval": "2m",
            "repeat_interval": "4h",
            "routes": per_recipient + catch_all,
        }
        # Always use the service account token for the provisioning API write —
        # it requires Admin role, which the SA has.  User basic_auth (Editor)
        # would 403 here regardless of the user's Grafana role.
        sa_headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=sa_headers, timeout=10.0
        ) as client:
            resp = await client.put("/api/v1/provisioning/policies", json=policy)
            if resp.status_code == 403 and "invalidProvenance" in resp.text:
                # Existing policy tree is file-provisioned; reset it first, then retry.
                reset_resp = await client.delete("/api/v1/provisioning/policies")
                reset_resp.raise_for_status()
                resp = await client.put("/api/v1/provisioning/policies", json=policy)
            resp.raise_for_status()

    async def get_notification_policy(self) -> dict | None:
        """Return the current notification policy from Grafana provisioning API.

        This attempts to GET `/api/v1/provisioning/policies` using the service
        account token. On error or if the policy is not present, return None.
        """
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=self._auth_headers, timeout=10.0
            ) as client:
                resp = await client.get("/api/v1/provisioning/policies")
                if resp.status_code != 200:
                    return None
                try:
                    body = resp.json()
                except Exception:
                    return None
                # Normalize to a dict when a single-item list is returned
                if isinstance(body, list) and len(body) == 1:
                    return body[0]
                return body
        except httpx.HTTPError:
            return None

    # ── Folders ───────────────────────────────────────────────────────────────

    async def ensure_folder(self, title: str) -> str:
        """Return the UID of a folder with *title*, creating it if absent."""
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            resp = await client.get("/api/folders", params={"limit": 100})
            resp.raise_for_status()
            for folder in resp.json():
                if folder.get("title") == title:
                    return folder["uid"]

            # Not found — create it
            resp = await client.post("/api/folders", json={"title": title})
            resp.raise_for_status()
            return resp.json()["uid"]

    # ── Contact points (recipients) ───────────────────────────────────────────

    async def list_contact_points(self, basic_auth: tuple[str, str] | None = None) -> list[dict]:
        """GET /api/v1/provisioning/contact-points"""
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=10.0, **self._auth_kwargs(basic_auth)
        ) as client:
            resp = await client.get("/api/v1/provisioning/contact-points")
            resp.raise_for_status()
            return resp.json()

    async def create_contact_point(self, name: str, email: str) -> dict:
        """Create an email contact point. X-Disable-Provenance keeps it UI-editable."""
        payload = {
            "name": name,
            "type": "email",
            "settings": {
                "addresses": email,
                "singleEmail": False,
            },
            "disableResolveMessage": False,
        }
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.post(
                "/api/v1/provisioning/contact-points", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_contact_point(self, uid: str) -> None:
        """DELETE a contact point by UID."""
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.delete(f"/api/v1/provisioning/contact-points/{uid}")
            resp.raise_for_status()

    async def sync_email_contact_points_single_email(self, enabled: bool = False) -> dict:
        """Force-update all email contact points to set settings.singleEmail.

        This is used as a one-shot remediation when existing contact points were
        created with grouped email mode and need to be normalized.
        """
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        updated = 0
        skipped = 0
        errors: list[dict] = []

        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.get("/api/v1/provisioning/contact-points")
            resp.raise_for_status()
            points: list[dict] = resp.json()

            for cp in points:
                if cp.get("type") != "email":
                    skipped += 1
                    continue

                uid = cp.get("uid", "")
                if not uid:
                    skipped += 1
                    continue

                settings = dict(cp.get("settings") or {})
                if settings.get("singleEmail") is enabled:
                    skipped += 1
                    continue

                settings["singleEmail"] = enabled
                payload = {
                    "uid": uid,
                    "name": cp.get("name", ""),
                    "type": "email",
                    "settings": settings,
                    "disableResolveMessage": cp.get("disableResolveMessage", False),
                }

                put_resp = await client.put(
                    f"/api/v1/provisioning/contact-points/{uid}", json=payload
                )
                if put_resp.status_code >= 400:
                    errors.append(
                        {
                            "uid": uid,
                            "status": put_resp.status_code,
                            "body": put_resp.text,
                        }
                    )
                    continue

                updated += 1

        return {"updated": updated, "skipped": skipped, "errors": errors}

    # ── Rule payload builder ──────────────────────────────────────────────────

    @staticmethod
    def build_rule_payload(
        *,
        title: str,
        fridge: str,
        metric: str,
        metric_label: str,
        metric_unit: str,
        grafana_operator: str,
        threshold: float,
        for_duration: str,
        folder_uid: str,
        expr: str | None = None,
    ) -> dict:
        """Construct the Grafana alert rule JSON body from simplified inputs.

        The metric name is validated by the caller against the allowlist before
        this method is called, so it is safe to interpolate into PromQL here.

        Pass expr to override the default `metric{instance="fridge"}` PromQL with a
        custom expression (e.g. for computed metrics like seconds_since_last_push).
        """
        resolved_expr = expr if expr is not None else f'{metric}{{instance="{fridge}"}}'
        data = [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": PROMETHEUS_DS_UID,
                "model": {
                    "expr": resolved_expr,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "refId": "A",
                },
            },
            {
                # Reduce the time-series to a single scalar before thresholding.
                # Without this step Grafana raises "looks like time series data,
                # only reduced data can be alerted on."
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
                        {
                            "evaluator": {
                                "type": grafana_operator,
                                "params": [threshold],
                            }
                        }
                    ],
                },
            },
        ]
        symbol = GRAFANA_TYPE_TO_SYMBOL.get(grafana_operator, grafana_operator)
        unit_str = f" {metric_unit}" if metric_unit else ""
        return {
            "title": title,
            "ruleGroup": "user-alerts",
            "folderUID": folder_uid,
            "condition": "C",
            "data": data,
            "for": for_duration,
            "noDataState": "NoData",
            "execErrState": "Error",
            "labels": {
                "severity": BASIC_SEVERITY,
                "fridge": fridge,
                "metric": metric,
                "managed_by": "alert-api",
                "rulename": title,
            },
            "annotations": {
                "summary": (
                    f"{metric_label} is {{{{ $values.B }}}}{unit_str}"
                    f" ({metric} {symbol} {threshold})"
                )
            },
        }

    # ── Response parsers ──────────────────────────────────────────────────────

    @staticmethod
    def parse_rule(rule: dict) -> dict:
        """Extract the simplified fields we expose from a raw Grafana rule dict."""
        # Parse metric from refId A expression.
        # NOTE: this assumes the expr is a simple `metric_name{labels}` form, as
        # produced by build_rule_payload. File-provisioned staleness rules use a
        # compound expression like `(time() - last_push_timestamp_seconds{...}) / 60`,
        # so split("{")[0] yields a garbled string starting with "(". The alert still
        # appears in the UI; it just shows a garbled metric column. Similarly, those
        # rules carry no `fridge` label, so fridge is returned as "". Non-fatal.
        metric = ""
        operator = ""
        threshold = 0.0
        try:
            for entry in rule.get("data", []):
                if entry.get("refId") == "A":
                    expr: str = entry.get("model", {}).get("expr", "")
                    metric = expr.split("{")[0]
                elif entry.get("refId") == "C":
                    conditions = (
                        entry.get("model", {})
                        .get("conditions", [{}])
                    )
                    if conditions:
                        evaluator = conditions[0].get("evaluator", {})
                        grafana_type = evaluator.get("type", "")
                        operator = GRAFANA_TYPE_TO_SYMBOL.get(grafana_type, grafana_type)
                        params = evaluator.get("params", [0.0])
                        threshold = float(params[0]) if params else 0.0
        except Exception:
            pass

        labels = rule.get("labels", {})
        raw_notify = labels.get("notify_to", "")
        notify_to = [u.strip() for u in raw_notify.split(",") if u.strip()]
        # Prefer the metric label stored at creation time over the expr-derived value.
        # For rules using a custom expr (e.g. seconds_since_last_push) the expr split
        # produces a garbled string; the label always has the clean metric name.
        metric = labels.get("metric") or metric
        return {
            "uid": rule.get("uid", ""),
            "title": rule.get("title", ""),
            "fridge": labels.get("fridge", ""),
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "enabled": not bool(rule.get("isPaused", False)),
            "provisioned": rule.get("provenance") == "file",
            "state": rule.get("_state", "unknown"),
            "notify_to": notify_to,
        }
