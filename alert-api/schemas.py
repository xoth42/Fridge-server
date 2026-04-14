import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class CreateAlertRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    fridge: str
    metric: str
    operator: str
    threshold: float
    for_duration: str = "1m"

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
    enabled: bool = True
    provisioned: bool
    state: str
    current_value: Optional[float] = None
    notify_to: list[str] = []
    recipient_count: int = 0


class SetAlertEnabledRequest(BaseModel):
    enabled: bool


class SetAlertRecipientsRequest(BaseModel):
    contact_uids: list[str]


class SetRecipientAutoSubscribeRequest(BaseModel):
    auto_subscribe: bool


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
    auto_subscribe: bool = True


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
