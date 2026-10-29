import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventCreate(BaseModel):
    event_type: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    payload: dict[str, Any]

    model_config = ConfigDict(extra="forbid")


class EventRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
