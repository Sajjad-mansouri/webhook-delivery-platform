import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Delivery(Base):
    __tablename__ = "deliveries"

    __table_args__ = (
        CheckConstraint(
            "attempts >= 0",
            name="attempts_nonnegative",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed')",
            name="valid_status",
        ),
        Index(
            "uq_deliveries_original_event_endpoint",
            "event_id",
            "endpoint_id",
            unique=True,
            postgresql_where=text("replay_of_delivery_id IS NULL"),
            sqlite_where=text("replay_of_delivery_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # NULL means this is an original delivery.
    # Otherwise, this delivery is a replay of the referenced delivery.
    replay_of_delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("deliveries.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )

    attempts: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        unique=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
