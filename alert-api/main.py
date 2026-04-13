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
)

# ── Config ────────────────────────────────────────────────────────────────────

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
GRAFANA_SA_TOKEN = os.environ.get("GRAFANA_SA_TOKEN", "")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")

# Populated at startup from metrics.yml
_metrics_config: dict = {}
_grafana: Optional[GrafanaClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _metrics_config, _grafana
    with open("metrics.yml") as fh:
        _metrics_config = yaml.safe_load(fh)
    _grafana = GrafanaClient(GRAFANA_URL, GRAFANA_SA_TOKEN)
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


async def _fetch_prometheus_value(metric: str, fridge: str) -> Optional[float]:
    try:
        query = f'{metric}{{instance="{fridge}"}}'
        async with httpx.AsyncClient(base_url=PROMETHEUS_URL, timeout=5.0) as client:
            resp = await client.get("/api/v1/query", params={"query": query})
            if resp.status_code == 200:
                results = resp.json().get("data", {}).get("result", [])
                if results:
                    return float(results[0]["value"][1])
    except Exception:
        pass
    return None


@app.get("/api/alerts", dependencies=[Depends(require_auth)])
async def list_alerts() -> list[AlertListItem]:
    try:
        raw_rules = await _grafana.list_alert_rules()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    items = [AlertListItem(**GrafanaClient.parse_rule(r)) for r in raw_rules]
    values = await asyncio.gather(*[_fetch_prometheus_value(i.metric, i.fridge) for i in items])
    for item, val in zip(items, values):
        item.current_value = val

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
        grafana_operator=grafana_operator,
        threshold=req.threshold,
        for_duration=req.for_duration,
        severity=req.severity,
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


@app.delete("/api/alerts/{uid}", dependencies=[Depends(require_auth)])
async def delete_alert(uid: str) -> dict:
    # Validate UID is a safe identifier (no path traversal)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise HTTPException(status_code=400, detail="Invalid alert UID")

    try:
        await _grafana.delete_alert_rule(uid)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Alert not found")
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    return {"deleted": True}


@app.get("/api/recipients", dependencies=[Depends(require_auth)])
async def list_recipients() -> list[RecipientListItem]:
    try:
        raw = await _grafana.list_contact_points()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Grafana error: {exc.response.status_code}")

    return [
        RecipientListItem(
            uid=cp.get("uid", ""),
            name=cp.get("name", ""),
            type=cp.get("type", ""),
        )
        for cp in raw
    ]


@app.post("/api/recipients", dependencies=[Depends(require_auth)], response_model=RecipientListItem)
async def create_recipient(req: CreateRecipientRequest) -> RecipientListItem:
    try:
        created = await _grafana.create_contact_point(req.name, req.email)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Grafana error {exc.response.status_code}: {exc.response.text}",
        )

    return RecipientListItem(
        uid=created.get("uid", ""),
        name=created.get("name", req.name),
        type=created.get("type", "email"),
    )
