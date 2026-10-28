import ipaddress
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


def validate_endpoint_url(value: AnyHttpUrl) -> AnyHttpUrl:
    parsed = urlsplit(str(value))

    if parsed.scheme != "https":
        raise ValueError("Endpoint URLs must use HTTPS.")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Endpoint URLs must not contain credentials.")

    if parsed.fragment:
        raise ValueError("Endpoint URLs must not contain fragments.")

    hostname = parsed.hostname

    if not hostname:
        raise ValueError("Endpoint URL must include a hostname.")

    normalized_hostname = hostname.rstrip(".").lower()

    if (
        normalized_hostname == "localhost"
        or normalized_hostname.endswith(".localhost")
        or normalized_hostname.endswith(".local")
        or normalized_hostname.endswith(".internal")
    ):
        raise ValueError("Local or internal hostnames are not allowed.")

    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        # This is a hostname, not an IP address literal.
        pass
    else:
        if not address.is_global:
            raise ValueError("Non-public IP addresses are not allowed.")

    return value


class EndpointCreate(BaseModel):
    url: AnyHttpUrl
    description: str | None = Field(default=None, max_length=200)

    model_config = ConfigDict(extra="forbid")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return validate_endpoint_url(value)

    @field_validator("description")
    @classmethod
    def normalize_description(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        value = value.strip()
        return value or None


class EndpointUpdate(BaseModel):
    url: AnyHttpUrl | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("url")
    @classmethod
    def validate_url(
        cls,
        value: AnyHttpUrl | None,
    ) -> AnyHttpUrl | None:
        if value is None:
            return None

        return validate_endpoint_url(value)

    @field_validator("description")
    @classmethod
    def normalize_description(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        value = value.strip()
        return value or None


class EndpointRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    url: AnyHttpUrl
    description: str | None
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
