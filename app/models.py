"""SQLAlchemy models. These mirror the DDL in migrations/versions/0001_initial.py
exactly (generated columns included as read-only mapped columns) so the ORM
layer can be used for reads, while all writes on the hot path go through
raw SQL in app/services/ for full control over ON CONFLICT semantics.
"""
import uuid

from sqlalchemy import (
    CHAR,
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import relationship

from app.core.db import Base

EventTypeEnum = ENUM(
    "payment_initiated",
    "payment_processed",
    "payment_failed",
    "settled",
    name="event_type_enum",
    create_type=False,  # created explicitly in the migration
)


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id = Column(Text, primary_key=True)
    merchant_name = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False)


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(UUID(as_uuid=True), nullable=False)
    merchant_id = Column(Text, ForeignKey("merchants.merchant_id"), nullable=False)
    event_type = Column(EventTypeEnum, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    event_timestamp = Column(TIMESTAMP(timezone=True), nullable=False)
    received_at = Column(TIMESTAMP(timezone=True), nullable=False)
    raw_payload = Column(JSONB, nullable=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_event_amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="chk_event_currency_format"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    transaction_id = Column(UUID(as_uuid=True), primary_key=True)
    merchant_id = Column(Text, ForeignKey("merchants.merchant_id"), nullable=False)
    currency = Column(CHAR(3), nullable=False)

    initiated_at = Column(TIMESTAMP(timezone=True))
    processed_at = Column(TIMESTAMP(timezone=True))
    failed_at = Column(TIMESTAMP(timezone=True))
    settled_at = Column(TIMESTAMP(timezone=True))
    last_settled_at = Column(TIMESTAMP(timezone=True))
    first_event_at = Column(TIMESTAMP(timezone=True), nullable=False)
    last_event_at = Column(TIMESTAMP(timezone=True), nullable=False)

    initiated_amount = Column(Numeric(14, 2))
    processed_amount = Column(Numeric(14, 2))
    settlement_amount = Column(Numeric(14, 2))

    event_count = Column(Integer, nullable=False, default=0)
    initiated_event_count = Column(Integer, nullable=False, default=0)
    processed_event_count = Column(Integer, nullable=False, default=0)
    failed_event_count = Column(Integer, nullable=False, default=0)
    settled_event_count = Column(Integer, nullable=False, default=0)

    created_at = Column(TIMESTAMP(timezone=True), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False)

    # Generated columns (DB-computed; never written by the app)
    payment_amount = Column(Numeric(14, 2))
    payment_status = Column(Text)
    settlement_status = Column(Text)
    status = Column(Text)

    merchant = relationship("Merchant")

    __table_args__ = (CheckConstraint("event_count > 0", name="chk_counts"),)
