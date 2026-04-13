"""Thin async wrapper around the Grafana HTTP API used by alert-api."""

import httpx

# Prometheus datasource UID — hardcoded, defined in provisioning/datasources/prometheus.yml
PROMETHEUS_DS_UID = "P1809F7CD0C75ACF3"

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
        """DELETE an alert rule. Raises ValueError if the rule is provisioned."""
        rule = await self.get_alert_rule(uid)
        if rule.get("provenance") == "file":
            raise PermissionError("Cannot delete a provisioned (baseline) alert")

        headers = {**self._auth_headers, "X-Disable-Provenance": "true"}
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=10.0
        ) as client:
            resp = await client.delete(f"/api/v1/provisioning/alert-rules/{uid}")
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
                "singleEmail": True,
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

    # ── Rule payload builder ──────────────────────────────────────────────────

    @staticmethod
    def build_rule_payload(
        *,
        title: str,
        fridge: str,
        metric: str,
        grafana_operator: str,
        threshold: float,
        for_duration: str,
        severity: str,
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
                "refId": "C",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "type": "threshold",
                    "expression": "A",
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
        return {
            "title": title,
            "ruleGroup": "user-alerts",
            "folderUID": folder_uid,
            "condition": "C",
            "data": data,
            "for": for_duration,
            "noDataState": "Alerting",
            "execErrState": "Alerting",
            "labels": {
                "severity": severity,
                "fridge": fridge,
                "managed_by": "alert-api",
            },
            "annotations": {
                "summary": (
                    f"{fridge} {metric} is {{{{ $values.A }}}} "
                    f"(threshold: {symbol} {threshold})"
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
        return {
            "uid": rule.get("uid", ""),
            "title": rule.get("title", ""),
            "fridge": labels.get("fridge", ""),
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "severity": labels.get("severity", ""),
            "provisioned": rule.get("provenance") == "file",
            "state": rule.get("_state", "unknown"),
        }
