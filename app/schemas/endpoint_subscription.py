import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionCreate(BaseModel):
    event_type: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )

    model_config = ConfigDict(extra="forbid")


class SubscriptionRead(BaseModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
