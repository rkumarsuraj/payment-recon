"""GET /transactions, GET /transactions/{transaction_id}.

All filtering, sorting, pagination, and counting happen in SQL. Python only
maps rows to Pydantic models.
"""
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services.reconciliation import HAS_DISCREPANCY_SQL, query_discrepancies

router = APIRouter(tags=["transactions"])

# Whitelists -- user-supplied strings never reach SQL as raw column names.
_SORT_COLUMNS = {
    "initiated_at": "t.initiated_at",
    "last_event_at": "t.last_event_at",
    "payment_amount": "t.payment_amount",
    "status": "t.status",
}
_DATE_FIELDS = {
    "initiated_at": "t.initiated_at",
    "last_event_at": "t.last_event_at",
    "settled_at": "t.settled_at",
}


@router.get("/transactions")
def list_transactions(
    merchant_id: Optional[list[str]] = Query(default=None),
    status_: Optional[list[str]] = Query(default=None, alias="status"),
    payment_status: Optional[str] = Query(default=None),
    settlement_status: Optional[str] = Query(default=None),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    date_field: str = Query(default="initiated_at"),
    min_amount: Optional[float] = Query(default=None),
    max_amount: Optional[float] = Query(default=None),
    has_discrepancy: Optional[bool] = Query(default=None),
    sort_by: str = Query(default="initiated_at"),
    sort_order: str = Query(default="desc"),
    limit: int = Query(default=None),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if date_field not in _DATE_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"date_field must be one of {list(_DATE_FIELDS)}"},
        )
    if sort_by not in _SORT_COLUMNS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"sort_by must be one of {list(_SORT_COLUMNS)}"},
        )
    if sort_order.lower() not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "sort_order must be 'asc' or 'desc'"},
        )

    limit = limit or settings.DEFAULT_PAGE_SIZE
    if limit > settings.MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"limit must be <= {settings.MAX_PAGE_SIZE}"},
        )

    date_col = _DATE_FIELDS[date_field]
    sort_col = _SORT_COLUMNS[sort_by]
    direction = "ASC" if sort_order.lower() == "asc" else "DESC"

    from_ts = _parse_date(from_) if from_ else None
    to_ts = _parse_date(to) if to else None
    if from_ts and to_ts and from_ts > to_ts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "'from' must be before 'to'"},
        )

    sql = f"""
        SELECT
            t.transaction_id, t.merchant_id, m.merchant_name,
            t.status, t.payment_status, t.settlement_status, t.currency,
            t.payment_amount, t.settlement_amount,
            t.initiated_at, t.processed_at, t.failed_at, t.settled_at,
            t.event_count,
            {HAS_DISCREPANCY_SQL} AS has_discrepancy,
            count(*) OVER () AS total_count
        FROM transactions t
        JOIN merchants m USING (merchant_id)
        WHERE (:merchant_ids IS NULL OR t.merchant_id = ANY(:merchant_ids ::text[]))
          AND (:statuses IS NULL OR t.status = ANY(:statuses ::text[]))
          AND (:payment_status IS NULL OR t.payment_status = :payment_status)
          AND (:settlement_status IS NULL OR t.settlement_status = :settlement_status)
          AND (:from_ts IS NULL OR {date_col} >= :from_ts)
          AND (:to_ts IS NULL OR {date_col} < :to_ts)
          AND (:min_amount IS NULL OR t.payment_amount >= :min_amount)
          AND (:max_amount IS NULL OR t.payment_amount <= :max_amount)
        ORDER BY {sort_col} {direction}, t.transaction_id
        LIMIT :limit OFFSET :offset;
    """

    params = {
        "merchant_ids": merchant_id,
        "statuses": status_,
        "payment_status": payment_status,
        "settlement_status": settlement_status,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "min_amount": min_amount,
        "max_amount": max_amount,
        "unsettled_after_hours": settings.UNSETTLED_AFTER_HOURS,
        "stuck_after_hours": settings.STUCK_AFTER_HOURS,
        "limit": limit,
        "offset": offset,
    }
    rows = db.execute(text(sql), params).mappings().all()

    if has_discrepancy is not None:
        rows = [r for r in rows if r["has_discrepancy"] == has_discrepancy]

    total = rows[0]["total_count"] if rows else 0
    data = [
        {
            "transaction_id": r["transaction_id"],
            "merchant_id": r["merchant_id"],
            "merchant_name": r["merchant_name"],
            "status": r["status"],
            "payment_status": r["payment_status"],
            "settlement_status": r["settlement_status"],
            "currency": r["currency"],
            "payment_amount": r["payment_amount"],
            "settlement_amount": r["settlement_amount"],
            "initiated_at": r["initiated_at"],
            "processed_at": r["processed_at"],
            "failed_at": r["failed_at"],
            "settled_at": r["settled_at"],
            "event_count": r["event_count"],
            "has_discrepancy": r["has_discrepancy"],
        }
        for r in rows
    ]
    return {
        "data": data,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": offset + len(data) < total,
        },
    }


@router.get("/transactions/{transaction_id}")
def get_transaction(transaction_id: uuid.UUID, db: Session = Depends(get_db)):
    txn_sql = text(
        f"""
        SELECT
            t.transaction_id, t.merchant_id, m.merchant_name,
            t.status, t.payment_status, t.settlement_status, t.currency,
            t.payment_amount, t.settlement_amount,
            t.initiated_at, t.processed_at, t.failed_at, t.settled_at,
            t.event_count, t.settled_event_count,
            {HAS_DISCREPANCY_SQL} AS has_discrepancy
        FROM transactions t
        JOIN merchants m USING (merchant_id)
        WHERE t.transaction_id = :tid;
        """
    )
    row = db.execute(
        txn_sql,
        {
            "tid": str(transaction_id),
            "unsettled_after_hours": settings.UNSETTLED_AFTER_HOURS,
            "stuck_after_hours": settings.STUCK_AFTER_HOURS,
        },
    ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TRANSACTION_NOT_FOUND", "message": f"No transaction with id {transaction_id}"},
        )

    events_sql = text(
        """
        SELECT event_id, event_type, amount, currency, event_timestamp, received_at
        FROM payment_events
        WHERE transaction_id = :tid
        ORDER BY event_timestamp ASC, received_at ASC;
        """
    )
    event_rows = db.execute(events_sql, {"tid": str(transaction_id)}).mappings().all()

    # A replay is a later insertion attempt for an event_id already seen --
    # but by definition a replay never lands a second row in payment_events
    # (ON CONFLICT DO NOTHING), so from this table alone every row here is
    # a distinct, accepted event. "Replay" visibility instead means: does
    # this transaction have more events of a type than the happy path
    # expects? We flag the 2nd+ event of any type as such.
    seen_types: dict[str, int] = {}
    events_out = []
    for r in event_rows:
        seen_types[r["event_type"]] = seen_types.get(r["event_type"], 0) + 1
        events_out.append(
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "amount": r["amount"],
                "currency": r["currency"],
                "event_timestamp": r["event_timestamp"],
                "received_at": r["received_at"],
                "is_duplicate_replay": seen_types[r["event_type"]] > 1,
            }
        )

    from_amount = row["payment_amount"]
    to_amount = row["settlement_amount"]
    amount_delta = (to_amount - from_amount) if (to_amount is not None and from_amount is not None) else None

    # Reuse the exact same SQL rules the discrepancies endpoint uses, scoped
    # to this one transaction, so there is a single source of truth for
    # what counts as a break -- no separate Python re-implementation to
    # drift out of sync with the six rules in app/services/reconciliation.py.
    break_rows, _, _ = query_discrepancies(
        db,
        merchant_ids=None,
        types=None,
        severity=None,
        from_ts=None,
        to_ts=None,
        unsettled_after_hours=settings.UNSETTLED_AFTER_HOURS,
        stuck_after_hours=settings.STUCK_AFTER_HOURS,
        sort_by="type",
        limit=10,
        offset=0,
        transaction_id=str(transaction_id),
    )
    discrepancies = [
        {"type": b["discrepancy_type"], "severity": b["severity"], "reason": b["reason"]} for b in break_rows
    ]

    by_type: dict[str, int] = {}
    for r in event_rows:
        by_type[r["event_type"]] = by_type.get(r["event_type"], 0) + 1

    txn_out = dict(row)
    txn_out.pop("settled_event_count", None)  # internal-only, used for discrepancy detection above

    return {
        "transaction": txn_out,
        "merchant": {"merchant_id": row["merchant_id"], "merchant_name": row["merchant_name"]},
        "reconciliation": {
            "is_reconciled": len(discrepancies) == 0,
            "discrepancies": discrepancies,
            "amount_delta": amount_delta if amount_delta is not None else 0,
        },
        "events": events_out,
        "event_summary": {"total": len(event_rows), "by_type": by_type},
    }


def _parse_date(value: str):
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "BAD_REQUEST", "message": f"Invalid date/datetime: {value}"},
            )
