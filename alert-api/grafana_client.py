"""Thin async wrapper around the Grafana HTTP API used by alert-api."""

import json
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

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {token}"}

    # ── Auth validation ───────────────────────────────────────────────────────

    async def validate_credentials(self, username: str, password: str) -> bool:
        """Return True if the Grafana username/password are valid (any role)."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            resp = await client.get("/api/user", auth=(username, password))
            return resp.status_code == 200

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
        self, alert_items: list[dict]
    ) -> None:
        """Regenerate the Grafana notification policy so that:
        - Alerts with a notify_to label go only to the listed contact points.
        - Alerts without notify_to fall through to the default catch-all.
        """
        # Collect which contact UIDs are actively referenced
        active_uids: set[str] = set()
        for item in alert_items:
            for uid in item.get("notify_to", []):
                if uid:
                    active_uids.add(uid)

        # Get contact point name for every known UID
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
        ) as client:
            cp_resp = await client.get("/api/v1/provisioning/contact-points")
            cp_resp.raise_for_status()
            uid_to_name = {
                cp.get("uid", ""): cp.get("name", "")
                for cp in cp_resp.json()
            }

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

        # Catch-all routes — only fire when notify_to is absent or empty.
        # When per_recipient is non-empty we add the notify_to!~'.+' guard so
        # assigned alerts don't also hit the catch-all.
        if per_recipient:
            catch_all: list[dict] = [
                {
                    "receiver": "lab-slack",
                    "continue": True,
                    "object_matchers": [["notify_to", "!~", ".+"]],
                },
                {
                    "receiver": "lab-email",
                    "object_matchers": [["notify_to", "!~", ".+"]],
                },
            ]
        else:
            catch_all = [
                {"receiver": "lab-slack", "continue": True},
                {"receiver": "lab-email"},
            ]

        policy = {
            "receiver": "lab-email",
            "group_by": ["alertname", "fridge"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": per_recipient + catch_all,
        }
        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.put("/api/v1/provisioning/policies", json=policy)
            resp.raise_for_status()

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

    async def list_contact_points(self) -> list[dict]:
        """GET /api/v1/provisioning/contact-points"""
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers, timeout=10.0
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
    ) -> dict:
        """Construct the Grafana alert rule JSON body from simplified inputs.

        The metric name is validated by the caller against the allowlist before
        this method is called, so it is safe to interpolate into PromQL here.
        """
        data = [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": PROMETHEUS_DS_UID,
                "model": {
                    "expr": f'{metric}{{instance="{fridge}"}}',
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
        # Parse metric from refId A expression
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
