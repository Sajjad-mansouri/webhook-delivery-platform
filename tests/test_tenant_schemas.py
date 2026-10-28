import uuid
from datetime import UTC, datetime

import pytest
from app.models import Tenant
from app.schemas import TenantCreate, TenantRead
from pydantic import ValidationError


class TestTenantCreateSchema:
    def test_valid_tenant_input_is_accepted(self) -> None:
        schema = TenantCreate(
            name="Example Organization",
            slug="example-org",
        )

        assert schema.name == "Example Organization"
        assert schema.slug == "example-org"

    def test_name_whitespace_is_trimmed(self) -> None:
        schema = TenantCreate(
            name="  Example Organization  ",
            slug="example-org",
        )

        assert schema.name == "Example Organization"

    @pytest.mark.parametrize(
        "slug",
        [
            "Example-Org",
            "example org",
            "-example",
            "example-",
            "example--org",
            "example_org",
        ],
    )
    def test_invalid_slug_is_rejected(self, slug: str) -> None:
        with pytest.raises(ValidationError):
            TenantCreate(
                name="Example Organization",
                slug=slug,
            )

    def test_blank_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TenantCreate(
                name="   ",
                slug="example-org",
            )

    def test_unexpected_input_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TenantCreate.model_validate(
                {
                    "name": "Example Organization",
                    "slug": "example-org",
                    "id": str(uuid.uuid4()),
                }
            )


class TestTenantReadSchema:
    def test_schema_reads_attributes_from_orm_model(self) -> None:
        tenant_id = uuid.uuid4()
        created_at = datetime.now(UTC)

        tenant = Tenant(
            id=tenant_id,
            name="Example Organization",
            slug="example-org",
            created_at=created_at,
        )

        schema = TenantRead.model_validate(tenant)

        assert schema.id == tenant_id
        assert schema.name == "Example Organization"
        assert schema.slug == "example-org"
        assert schema.created_at == created_at
