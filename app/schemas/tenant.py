import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(
        min_length=1, max_length=150, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class TenantRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
