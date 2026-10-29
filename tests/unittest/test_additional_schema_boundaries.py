import pytest
from app.schemas.endpoint_subscription import SubscriptionCreate
from app.schemas.errors import ErrorInfo, ErrorResponse, FieldError
from app.schemas.event import EventCreate
from pydantic import ValidationError


class TestSubscriptionCreateSchemaBoundaries:
    @pytest.mark.parametrize(
        "event_type",
        ["invoice.created", "user-registered", "v1_event", "A1"],
    )
    def test_accepts_event_types_matching_the_declared_pattern(self, event_type):
        assert SubscriptionCreate(event_type=event_type).event_type == event_type

    @pytest.mark.parametrize(
        "event_type",
        [
            "",
            ".starts-with-dot",
            "-starts-with-hyphen",
            "_starts-with-underscore",
            "has space",
        ],
    )
    def test_rejects_event_types_outside_the_declared_pattern(self, event_type):
        with pytest.raises(ValidationError):
            SubscriptionCreate(event_type=event_type)

    def test_rejects_event_type_longer_than_200_characters(self):
        with pytest.raises(ValidationError):
            SubscriptionCreate(event_type="a" * 201)

    def test_rejects_unexpected_fields(self):
        with pytest.raises(ValidationError):
            SubscriptionCreate(event_type="invoice.created", extra_field=True)


class TestEventCreateSchemaBoundaries:
    def test_accepts_empty_object_payload(self):
        model = EventCreate(event_type="invoice.created", payload={})
        assert model.payload == {}

    def test_rejects_missing_payload(self):
        with pytest.raises(ValidationError):
            EventCreate(event_type="invoice.created")

    def test_rejects_event_type_longer_than_200_characters(self):
        with pytest.raises(ValidationError):
            EventCreate(event_type="a" * 201)

    def test_rejects_unexpected_fields(self):
        with pytest.raises(ValidationError):
            EventCreate(
                event_type="invoice.created",
                payload={},
                tenant_id="client-supplied",
            )


class TestErrorResponseSchema:
    def test_accepts_standard_error_payload(self):
        response = ErrorResponse(
            error=ErrorInfo(
                code="not_found",
                message="Resource not found.",
                details=[
                    FieldError(
                        location=["body", "event_type"],
                        message="Field required",
                        type="missing",
                    )
                ],
            )
        )
        assert response.error.code == "not_found"
        assert response.error.details[0].location == ["body", "event_type"]

    def test_rejects_unexpected_top_level_fields(self):
        with pytest.raises(ValidationError):
            ErrorResponse(
                error={"code": "not_found", "message": "Resource not found."},
                unexpected=True,
            )
