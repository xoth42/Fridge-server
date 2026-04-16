"""alert-api — FastAPI service that proxies simplified alert operations to Grafana.

Authentication: callers must supply a valid Grafana username/password as HTTP
Basic auth on every request (except /api/health and /api/metrics).  The API
validates the credentials against Grafana's own /api/user endpoint, then
performs Grafana operations using the service account token (GRAFANA_SA_TOKEN).
This means:
  - Any Grafana user (Viewer, Editor, Admin) can authenticate.
  - The service account does the actual write operations so Editor-level lab
    accounts don't need elevated Grafana API permissions themselves.
  - No separate ALERT_API_SECRET or Caddy basicauth layer is needed.
"""

import base64
import asyncio
import os
import re
import yaml
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from grafana_client import GrafanaClient
from schemas import (
    AlertListItem,
    CreateAlertRequest,
    CreateAlertResponse,
    CreateRecipientRequest,
    MetricItem,
    MetricsResponse,
    FridgeItem,
    OperatorItem,
    RecipientListItem,
    SetAlertEnabledRequest,
    SetAlertRecipientsRequest,
    SetRecipientAutoSubscribeRequest,
)

# ── Config ────────────────────────────────────────────────────────────────────

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
GRAFANA_SA_TOKEN = os.environ.get("GRAFANA_SA_TOKEN", "")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
GRAFANA_RECEIVER_EMAIL = os.environ.get("GRAFANA_RECEIVER_EMAIL", "lab-email")
GRAFANA_RECEIVER_SLACK = os.environ.get("GRAFANA_RECEIVER_SLACK", "lab-slack")
_hidden_raw = os.environ.get("GRAFANA_HIDDEN_CONTACT_NAMES", "")
GRAFANA_HIDDEN_CONTACT_NAMES: set[str] = {
    n.strip() for n in _hidden_raw.split(",") if n.strip()
}

# Populated at startup from metrics.yml
_metrics_config: dict = {}
_grafana: Optional[GrafanaClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _metrics_config, _grafana
    with open("metrics.yml") as fh:
        _metrics_config = yaml.safe_load(fh)
    _grafana = GrafanaClient(
        GRAFANA_URL,
        GRAFANA_SA_TOKEN,
        receiver_email=GRAFANA_RECEIVER_EMAIL,
        receiver_slack=GRAFANA_RECEIVER_SLACK,
    )
    yield


app = FastAPI(title="Fridge Alert API", lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _allowed_metric_names() -> set[str]:
    return {m["name"] for m in _metrics_config.get("metrics", [])}


def _allowed_fridge_ids() -> set[str]:
    return {f["id"] for f in _metrics_config.get("fridges", [])}


def _operator_symbol_to_grafana(symbol: str) -> str:
    for op in _metrics_config.get("operators", []):
        if op["symbol"] == symbol:
            return op["grafana_type"]
    raise ValueError(f"Unknown operator: {symbol}")


def _metric_config_for(name: str) -> Optional[dict]:
    for m in _metrics_config.get("metrics", []):
        if m["name"] == name:
            return m
    return None


def _basic_creds_from_auth_header(authorization: Optional[str]) -> tuple[str, str]:
    """Parse Basic authorization header into (username, password)."""
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Grafana credentials required")
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid credentials format")
    if not username or not password:
        raise HTTPException(status_code=401, detail="Username and password required")
    return username, password


# ── Auth dependency ───────────────────────────────────────────────────────────

async def require_auth(
    authorization: Annotated[Optional[str], Header()] = None,
) -> None:
    """Validate HTTP Basic auth credentials against Grafana /api/user.

    We intentionally do NOT send WWW-Authenticate in 401 responses so that
    browsers don't show their native credential dialog — the frontend handles
    login with its own modal.
    """
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Grafana credentials required")

    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid credentials format")

    if not username or not password:
        raise HTTPException(status_code=401, detail="Username and password required")

    if not await _grafana.validate_credentials(username, password):
        raise HTTPException(status_code=401, detail="Invalid Grafana credentials")


async def require_admin_auth(
    authorization: Annotated[Optional[str], Header()] = None,
) -> None:
    """Validate HTTP Basic auth and require Grafana Admin role."""
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Grafana credentials required")

    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid credentials format")

    if not username or not password:
        raise HTTPException(status_code=401, detail="Username and password required")

    # Keep 401 for invalid credentials, 403 for authenticated but not admin.
    if not await _grafana.validate_credentials(username, password):
        raise HTTPException(status_code=401, detail="Invalid Grafana credentials")
    if not await _grafana.validate_admin_credentials(username, password):
        raise HTTPException(status_code=403, detail="Grafana admin credentials required")


AuthDep = Annotated[None, Depends(require_auth)]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict:
    """Public — used by Docker healthcheck and monitoring."""
    grafana_ok = await _grafana.health()
    return {
        "status": "ok",
        "grafana": "reachable" if grafana_ok else "unreachable",
    }


@app.get("/api/metrics", response_model=MetricsResponse)
async def get_metrics() -> MetricsResponse:
    """Public — returns the allowed metric/fridge/operator config for the frontend."""
    return MetricsResponse(
        metrics=[MetricItem(**m) for m in _metrics_config.get("metrics", [])],
        fridges=[FridgeItem(**f) for f in _metrics_config.get("fridges", [])],
        operators=[OperatorItem(**op) for op in _metrics_config.get("operators", [])],
    )


@app.get("/api/policy")
async def get_policy() -> dict:
    """Public — return the notification policy defaults used by the API.

    This mirrors the values used when rebuilding the Grafana notification
    policy so the frontend can display the live refire (repeat) interval.
    """
    defaults = {"group_wait": "10s", "group_interval": "2m", "repeat_interval": "4h"}
    # Try to fetch live policy from Grafana; fall back to defaults on any error.
    try:
        policy = await _grafana.get_notification_policy()
    except Exception:
        policy = None

    if not policy:
        return defaults

    p = policy[0] if isinstance(policy, list) and policy else (policy if isinstance(policy, dict) else None)
    if not p:
        return defaults

    group_wait = p.get("group_wait") or p.get("groupWait") or defaults["group_wait"]
    group_interval = p.get("group_interval") or p.get("groupInterval") or defaults["group_interval"]
    repeat_interval = p.get("repeat_interval") or p.get("repeatInterval") or defaults["repeat_interval"]

    return {
        "group_wait": group_wait,
        "group_interval": group_interval,
        "repeat_interval": repeat_interval,
    }


async def _fetch_prometheus_value(metric: str, fridge: str) -> Optional[float]:
    try:
        async with httpx.AsyncClient(base_url=PROMETHEUS_URL, timeout=5.0) as client:
            # First try the exact selector used in alert expressions.
            exact_query = f'{metric}{{instance="{fridge}"}}'
            resp = await client.get("/api/v1/query", params={"query": exact_query})
            if resp.status_code == 200:
                results = resp.json().get("data", {}).get("result", [])
                if results:
                    return float(results[0]["value"][1])

            # Fallback: query by metric and choose a series tied to this fridge.
            resp = await client.get("/api/v1/query", params={"query": metric})
            if resp.status_code == 200:
                results = resp.json().get("data", {}).get("result", [])
                for series in results:
                    labels = series.get("metric", {})
                    if labels.get("instance") == fridge or labels.get("fridge") == fridge:
                        return float(series["value"][1])
    except Exception:
        pass
    return None


@app.get("/api/alerts", dependencies=[Depends(require_auth)])
async def list_alerts() -> list[AlertListItem]:
    try:
        raw_rules = await _grafana.list_alert_rules()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    # Merge in disabled rules from the Grafana annotation store.
    # Mark each as isPaused so parse_rule returns enabled=False.
    try:
        disabled = await _grafana.list_disabled_rules()
        active_uids = {r.get("uid") for r in raw_rules}
        for rule in disabled:
            if rule.get("uid") not in active_uids:
                rule["isPaused"] = True
                rule["_state"] = "unknown"
                raw_rules.append(rule)
    except Exception:
        pass  # don't break the list if annotation store is unavailable

    items = [AlertListItem(**GrafanaClient.parse_rule(r)) for r in raw_rules]
    values = await asyncio.gather(*[_fetch_prometheus_value(i.metric, i.fridge) for i in items])
    for item, val in zip(items, values):
        item.current_value = val

    # Compute recipient_count per alert.
    # - Alerts with explicit notify_to → count = len(notify_to)
    # - Alerts with empty notify_to → count = number of auto-subscribe recipients
    try:
        auto_settings = await _grafana.get_auto_subscribe_settings()
        raw_cps = await _grafana.list_contact_points()
        auto_count = sum(
            1 for cp in raw_cps
            if cp.get("type") == "email" and auto_settings.get(cp.get("uid", ""), True)
        )
    except Exception:
        auto_settings = {}
        auto_count = 0

    for item in items:
        item.recipient_count = len(item.notify_to) if item.notify_to else auto_count

    return items


@app.post("/api/alerts", dependencies=[Depends(require_auth)], response_model=CreateAlertResponse)
async def create_alert(req: CreateAlertRequest) -> CreateAlertResponse:
    # ── Validate fridge ───────────────────────────────────────────────────────
    if req.fridge not in _allowed_fridge_ids():
        raise HTTPException(status_code=400, detail=f"Unknown fridge: {req.fridge!r}")

    # ── Validate metric (PromQL injection prevention) ─────────────────────────
    # The allowlist check is the primary guard; the regex is a belt-and-suspenders
    # defence ensuring the name is a plain identifier with no shell/PromQL metacharacters.
    if req.metric not in _allowed_metric_names():
        raise HTTPException(status_code=400, detail=f"Unknown metric: {req.metric!r}")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", req.metric):
        raise HTTPException(status_code=400, detail="Metric name contains invalid characters")

    # ── Validate fridge ID for the same reason ────────────────────────────────
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", req.fridge):
        raise HTTPException(status_code=400, detail="Fridge ID contains invalid characters")

    # ── Check metric-fridge compatibility ─────────────────────────────────────
    mc = _metric_config_for(req.metric)
    if mc and mc.get("fridges") is not None:
        if req.fridge not in mc["fridges"]:
            raise HTTPException(
                status_code=400,
                detail=f"Metric {req.metric!r} is not available for fridge {req.fridge!r}",
            )

    # ── Map operator symbol → Grafana type ───────────────────────────────────
    try:
        grafana_operator = _operator_symbol_to_grafana(req.operator)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # ── Ensure destination folder exists ─────────────────────────────────────
    try:
        folder_uid = await _grafana.ensure_folder("User Alerts")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana folder error: {exc.response.status_code}")

    # ── Build and POST the rule ───────────────────────────────────────────────
    payload = GrafanaClient.build_rule_payload(
        title=req.name,
        fridge=req.fridge,
        metric=req.metric,
        metric_label=mc["label"] if mc else req.metric,
        metric_unit=mc["unit"] if mc else "",
        grafana_operator=grafana_operator,
        threshold=req.threshold,
        for_duration=req.for_duration,
        folder_uid=folder_uid,
    )

    try:
        created = await _grafana.create_alert_rule(payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Grafana create error {exc.response.status_code}: {exc.response.text}",
        )

    return CreateAlertResponse(uid=created["uid"], title=created["title"])


@app.patch("/api/alerts/{uid}/enabled", dependencies=[Depends(require_auth)])
async def set_alert_enabled(uid: str, req: SetAlertEnabledRequest) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise HTTPException(status_code=400, detail="Invalid alert UID")

    try:
        if req.enabled:
            # Restore: load from annotation store → POST to Grafana with same UID.
            rule = await _grafana.pop_disabled_rule(uid)
            if rule is None:
                raise HTTPException(status_code=404, detail="Alert not found in disabled store")
            # Strip read-only fields; keep uid so Grafana reuses it.
            for field in ("id", "provenance", "updated"):
                rule.pop(field, None)
            rule["isPaused"] = False
            try:
                await _grafana.create_alert_rule(rule)
            except httpx.HTTPStatusError as exc:
                # Roll back: put it back in the store so the rule isn't lost.
                await _grafana.store_disabled_rule(uid, rule)
                raise HTTPException(
                    status_code=502,
                    detail=f"Grafana restore error {exc.response.status_code}: {exc.response.text}",
                )
        else:
            # Disable: save to annotation store → DELETE from Grafana.
            try:
                rule = await _grafana.get_alert_rule(uid)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise HTTPException(status_code=404, detail="Alert not found")
                raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

            if rule.get("provenance") == "file":
                raise HTTPException(status_code=403, detail="Cannot disable a provisioned baseline alert")

            await _grafana.store_disabled_rule(uid, rule)
            try:
                await _grafana.delete_alert_rule(uid)
            except httpx.HTTPStatusError as exc:
                await _grafana._delete_disabled_annotation(uid)  # rollback
                raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    return {"uid": uid, "enabled": req.enabled}


@app.delete("/api/alerts/{uid}", dependencies=[Depends(require_auth)])
async def delete_alert(uid: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise HTTPException(status_code=400, detail="Invalid alert UID")

    # A disabled rule only exists in the annotation store, not in Grafana.
    disabled_rule = await _grafana.pop_disabled_rule(uid)
    if disabled_rule is not None:
        return {"deleted": True}

    try:
        rule = await _grafana.get_alert_rule(uid)
        if rule.get("provenance") == "file":
            raise HTTPException(status_code=403, detail="Cannot delete a baseline alert")
        await _grafana.delete_alert_rule(uid)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Alert not found")
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    return {"deleted": True}


@app.patch("/api/alerts/{uid}/recipients", dependencies=[Depends(require_auth)])
async def set_alert_recipients(
    uid: str,
    req: SetAlertRecipientsRequest,
    authorization: Annotated[Optional[str], Header()] = None,
) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise HTTPException(status_code=400, detail="Invalid alert UID")
    for cuid in req.contact_uids:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", cuid):
            raise HTTPException(status_code=400, detail=f"Invalid contact UID: {cuid!r}")

    try:
        await _grafana.set_alert_notify_to(uid, req.contact_uids)
        raw_rules = await _grafana.list_alert_rules()
        items = [GrafanaClient.parse_rule(r) for r in raw_rules]
        await _grafana.rebuild_notification_policy(items)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    return {"uid": uid, "contact_uids": req.contact_uids}


@app.delete("/api/recipients/{uid}", dependencies=[Depends(require_auth)])
async def delete_recipient(uid: str, authorization: Annotated[Optional[str], Header()] = None) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise HTTPException(status_code=400, detail="Invalid recipient UID")
    try:
        await _grafana.delete_contact_point(uid)
    except httpx.HTTPStatusError as exc:
        # If not found, mirror 404.
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Recipient not found")

        # 409 may indicate a provenance/usage conflict. Attempt safe remediation:
        #  - remove the contact UID from any alert rules' notify_to lists
        #  - rebuild the notification policy so the receiver is no longer referenced
        #  - retry deletion once
        if exc.response.status_code == 409:
            try:
                # Find alerts referencing this contact UID
                raw_rules = await _grafana.list_alert_rules()
                items = [GrafanaClient.parse_rule(r) for r in raw_rules]
                referencing = [r for r in items if uid in (r.get("notify_to") or [])]

                # Unassign the contact UID from each referencing alert
                for r in referencing:
                    new_uids = [u for u in (r.get("notify_to") or []) if u != uid]
                    # If new_uids is empty we send an empty list => catch-all (all recipients)
                    await _grafana.set_alert_notify_to(r["uid"], new_uids)

                # Rebuild policy excluding the target so Grafana no longer
                # references it (contact still exists at this point).
                raw_rules2 = await _grafana.list_alert_rules()
                items2 = [GrafanaClient.parse_rule(r) for r in raw_rules2]
                await _grafana.rebuild_notification_policy(items2, exclude_uids={uid})

                # Retry delete
                await _grafana.delete_contact_point(uid)
            except httpx.HTTPStatusError as exc2:
                # If deletion still fails with 409, the policy or contact is
                # truly protected (e.g. file-provisioned).
                if exc2.response.status_code == 409:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Recipient cannot be deleted: it is still referenced by the Grafana "
                            "notification policy or is file-provisioned. If it was created via "
                            "config/grafana/provisioning/alerting/contact-points.yml, remove it "
                            "there and restart Grafana. Otherwise use the admin rebuild-policy "
                            "endpoint to clear stale policy references, then retry."
                        ),
                    )
                raise HTTPException(status_code=502, detail=f"Grafana error: {exc2.response.status_code}")
            except Exception:
                # Non-HTTP errors in remediation path
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Recipient could not be deleted due to a provisioning or policy conflict. "
                        "Check Grafana provisioning files and alert rule assignments before retrying."
                    ),
                )
            # Remediation + retry succeeded — policy was already rebuilt above.
            return {"deleted": True}

        # Other Grafana errors (not 404, not 409)
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    # Rebuild policy so the deleted contact point is removed from catch-all routes.
    try:
        raw_rules = await _grafana.list_alert_rules()
        items = [GrafanaClient.parse_rule(r) for r in raw_rules]
        await _grafana.rebuild_notification_policy(items)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Recipient deleted but policy update failed: {exc.response.status_code}",
        )

    return {"deleted": True}


@app.get("/api/recipients", dependencies=[Depends(require_auth)])
async def list_recipients() -> list[RecipientListItem]:
    try:
        raw = await _grafana.list_contact_points()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    try:
        auto_settings = await _grafana.get_auto_subscribe_settings()
    except Exception:
        auto_settings = {}

    return [
        RecipientListItem(
            uid=cp.get("uid", ""),
            name=cp.get("name", ""),
            type=cp.get("type", ""),
            provisioned=(cp.get("provenance") == "file"),
            auto_subscribe=auto_settings.get(cp.get("uid", ""), True),
        )
        for cp in raw
        if cp.get("name", "") not in GRAFANA_HIDDEN_CONTACT_NAMES
    ]


@app.post("/api/recipients", dependencies=[Depends(require_auth)], response_model=RecipientListItem)
async def create_recipient(req: CreateRecipientRequest, authorization: Annotated[Optional[str], Header()] = None) -> RecipientListItem:
    try:
        created = await _grafana.create_contact_point(req.name, req.email)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Grafana error {exc.response.status_code}: {exc.response.text}",
        )

    # Rebuild notification policy so the new contact point is included in
    # the catch-all and starts receiving all unassigned alerts immediately.
    try:
        raw_rules = await _grafana.list_alert_rules()
        items = [GrafanaClient.parse_rule(r) for r in raw_rules]
        await _grafana.rebuild_notification_policy(items)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Recipient created but policy update failed: {exc.response.status_code}",
        )

    return RecipientListItem(
        uid=created.get("uid", ""),
        name=created.get("name", req.name),
        type=created.get("type", "email"),
    )


@app.post("/api/recipients/sync-email-format", dependencies=[Depends(require_auth)])
async def sync_email_contact_points_format() -> dict:
    """One-shot remediation: set singleEmail=false on all email contact points."""
    try:
        result = await _grafana.sync_email_contact_points_single_email(enabled=False)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Grafana sync error {exc.response.status_code}: {exc.response.text}",
        )

    return {
        "singleEmail": False,
        **result,
    }


@app.patch("/api/recipients/{uid}/auto-subscribe", dependencies=[Depends(require_auth)])
async def set_recipient_auto_subscribe(
    uid: str,
    req: SetRecipientAutoSubscribeRequest,
    authorization: Annotated[Optional[str], Header()] = None,
) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise HTTPException(status_code=400, detail="Invalid recipient UID")
    try:
        basic_auth = _basic_creds_from_auth_header(authorization)
        await _grafana.set_auto_subscribe(uid, req.auto_subscribe, basic_auth=basic_auth)
        # Rebuild policy so catch-all reflects new auto-subscribe state
        raw_rules = await _grafana.list_alert_rules()
        items = [GrafanaClient.parse_rule(r) for r in raw_rules]
        await _grafana.rebuild_notification_policy(items)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")
    return {"uid": uid, "auto_subscribe": req.auto_subscribe}


@app.post("/api/policy/rebuild", dependencies=[Depends(require_admin_auth)])
async def rebuild_policy() -> dict:
    """Force-rebuild the Grafana notification policy from all current contact points.

    Use this after install to repair any case where earlier policy PUTs failed
    (e.g. the SA was Editor-role and 403'd silently), leaving some recipients
    unrouted.  Safe to call any number of times — idempotent.
    """
    try:
        raw_rules = await _grafana.list_alert_rules()
        items = [GrafanaClient.parse_rule(r) for r in raw_rules]
        await _grafana.rebuild_notification_policy(items)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Policy rebuild failed ({exc.response.status_code}): {exc.response.text}",
        )
    return {"rebuilt": True}


@app.post("/api/recipients/check", dependencies=[Depends(require_admin_auth)])
async def check_all_recipients(authorization: Annotated[Optional[str], Header()] = None) -> dict:
    """Send a one-shot test email to all configured recipient addresses."""
    try:
        basic_auth = _basic_creds_from_auth_header(authorization)
        result = await _grafana.send_test_email_to_all_recipients(basic_auth=basic_auth)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Grafana check error {exc.response.status_code}: {exc.response.text}",
        )
    return result
