import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DeliveryAttemptRead(BaseModel):
    id: uuid.UUID
    attempt_number: int
    status_code: int | None
    error_message: str | None
    outcome: str
    started_at: datetime
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class DeliveryRead(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    replay_of_delivery_id: uuid.UUID | None
    status: str
    attempts: int
    available_at: datetime
    locked_until: datetime | None
    created_at: datetime
    attempt_history: list[DeliveryAttemptRead]

    model_config = ConfigDict(from_attributes=True)


class DeliveryReplayRead(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    endpoint_id: uuid.UUID
    replay_of_delivery_id: uuid.UUID
    status: str
    attempts: int
    available_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
