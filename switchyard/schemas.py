from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl

Scope = Literal["events:read", "events:write"]


class RegisterIn(BaseModel):
    organization: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    prefix: str
    scopes: list[str]
    rate_limit_per_minute: int
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    key: str


class WebhookIn(BaseModel):
    url: HttpUrl
    event_types: list[str] = []


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    event_types: list[str]
    active: bool
    created_at: datetime


class WebhookCreated(WebhookOut):
    secret: str


class EventIn(BaseModel):
    type: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.\-]+$")
    data: dict


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    endpoint_id: int
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    next_attempt_at: datetime
    delivered_at: datetime | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    payload: dict
    created_at: datetime
    deliveries: list[DeliveryOut]


class UsageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    api_key_id: int
    day: date
    requests: int


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    action: str
    target: str
    created_at: datetime
