"""Pydantic v2 request/response models."""
import datetime as dt
import decimal
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, condecimal, field_validator

from app.core.config import settings

Money = condecimal(gt=0, decimal_places=2, max_digits=14)


class EventType(str, Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


# ---------------------------------------------------------------- ingestion


class PaymentEventIn(BaseModel):
    event_id: uuid.UUID
    event_type: EventType
    transaction_id: uuid.UUID
    merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: Optional[str] = Field(default=None, max_length=256)
    amount: Money
    currency: str = Field(min_length=3, max_length=3)
    timestamp: dt.datetime

    @field_validator("currency")
    @classmethod
    def currency_upper_alpha(cls, v: str) -> str:
        if not v.isalpha() or not v.isupper() or len(v) != 3:
            raise ValueError("currency must be a 3-letter uppercase code, e.g. INR")
        return v

    @field_validator("timestamp")
    @classmethod
    def timestamp_tz_aware_and_not_future(cls, v: dt.datetime) -> dt.datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (include a UTC offset)")
        now = dt.datetime.now(dt.timezone.utc)
        tolerance = dt.timedelta(minutes=settings.FUTURE_TIMESTAMP_TOLERANCE_MINUTES)
        if v > now + tolerance:
            raise ValueError("timestamp is too far in the future")
        return v


class EventsBatchIn(BaseModel):
    events: list[PaymentEventIn] = Field(min_length=1)


class EventResult(BaseModel):
    event_id: str
    status: str  # inserted | duplicate | rejected
    message: Optional[str] = None
    error: Optional[dict] = None


class IngestResponse(BaseModel):
    received: int
    inserted: int
    duplicates: int
    rejected: int
    transactions_touched: int
    results: list[EventResult]


# ---------------------------------------------------------------- transactions


class MerchantOut(BaseModel):
    merchant_id: str
    merchant_name: str


class TransactionOut(BaseModel):
    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    status: str
    payment_status: str
    settlement_status: str
    currency: str
    payment_amount: Optional[decimal.Decimal] = None
    settlement_amount: Optional[decimal.Decimal] = None
    initiated_at: Optional[dt.datetime] = None
    processed_at: Optional[dt.datetime] = None
    failed_at: Optional[dt.datetime] = None
    settled_at: Optional[dt.datetime] = None
    event_count: int
    has_discrepancy: bool


class PaginationOut(BaseModel):
    limit: int
    offset: int
    total: int
    has_more: bool


class TransactionListOut(BaseModel):
    data: list[TransactionOut]
    pagination: PaginationOut


class PaymentEventOut(BaseModel):
    event_id: uuid.UUID
    event_type: str
    amount: decimal.Decimal
    currency: str
    event_timestamp: dt.datetime
    received_at: dt.datetime
    is_duplicate_replay: bool = False


class DiscrepancyRef(BaseModel):
    type: str
    severity: str
    reason: str


class ReconciliationInfo(BaseModel):
    is_reconciled: bool
    discrepancies: list[DiscrepancyRef]
    amount_delta: decimal.Decimal


class EventSummary(BaseModel):
    total: int
    by_type: dict[str, int]


class TransactionDetailOut(BaseModel):
    transaction: TransactionOut
    merchant: MerchantOut
    reconciliation: ReconciliationInfo
    events: list[PaymentEventOut]
    event_summary: EventSummary


# ---------------------------------------------------------------- reconciliation


class SummaryRow(BaseModel):
    merchant_id: Optional[str] = None
    merchant_name: Optional[str] = None
    bucket_date: Optional[dt.date] = None
    status: Optional[str] = None
    transaction_count: int
    total_payment_amount: decimal.Decimal
    total_settlement_amount: decimal.Decimal
    amount_delta: decimal.Decimal
    settled_count: int
    failed_count: int
    unsettled_count: int
    event_count: int
    is_total_row: bool = False


class SummaryOut(BaseModel):
    data: list[SummaryRow]
    parameters: dict[str, Any]
    pagination: PaginationOut


class DiscrepancyOut(BaseModel):
    transaction_id: uuid.UUID
    merchant_id: str
    merchant_name: str
    discrepancy_type: str
    severity: str
    status: str
    payment_amount: Optional[decimal.Decimal] = None
    settlement_amount: Optional[decimal.Decimal] = None
    amount_delta: Optional[decimal.Decimal] = None
    age_hours: Optional[float] = None
    reason: str


class DiscrepancyListOut(BaseModel):
    data: list[DiscrepancyOut]
    counts_by_type: dict[str, int]
    pagination: PaginationOut
    parameters: dict[str, Any]


class HealthOut(BaseModel):
    status: str
    database: str
    version: str
    event_count: Optional[int] = None
