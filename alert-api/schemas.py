import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class CreateAlertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    fridge: str
    metric: str
    operator: str
    threshold: float
    for_duration: str = "5m"
    severity: str = "warning"

    @field_validator("severity")
    @classmethod
    def check_severity(cls, v: str) -> str:
        if v not in ("warning", "critical"):
            raise ValueError("severity must be 'warning' or 'critical'")
        return v

    @field_validator("for_duration")
    @classmethod
    def check_duration(cls, v: str) -> str:
        if not re.match(r"^\d+[mhds]$", v):
            raise ValueError("for_duration must match pattern like '5m', '1h', '30s'")
        return v

    @field_validator("operator")
    @classmethod
    def check_operator(cls, v: str) -> str:
        if v not in (">", "<", ">=", "<="):
            raise ValueError("operator must be one of >, <, >=, <=")
        return v


class CreateAlertResponse(BaseModel):
    uid: str
    title: str


class AlertListItem(BaseModel):
    uid: str
    title: str
    fridge: str
    metric: str
    operator: str
    threshold: float
    severity: str
    provisioned: bool
    state: str
    current_value: Optional[float] = None


class CreateRecipientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    email: str

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v


class RecipientListItem(BaseModel):
    uid: str
    name: str
    type: str


class MetricItem(BaseModel):
    name: str
    label: str
    unit: str
    fridges: Optional[list[str]] = None


class FridgeItem(BaseModel):
    id: str
    label: str


class OperatorItem(BaseModel):
    symbol: str
    grafana_type: str


class MetricsResponse(BaseModel):
    metrics: list[MetricItem]
    fridges: list[FridgeItem]
    operators: list[OperatorItem]
