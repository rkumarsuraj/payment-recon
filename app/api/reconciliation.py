"""GET /reconciliation/summary, GET /reconciliation/discrepancies."""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services.reconciliation import query_discrepancies, query_summary

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

_GROUP_DIMS = {"merchant", "date", "status"}
_GRANULARITIES = {"day", "week", "month"}
_DISCREPANCY_TYPES = {
    "PROCESSED_NOT_SETTLED",
    "SETTLED_AFTER_FAILURE",
    "DOUBLE_SETTLEMENT",
    "AMOUNT_MISMATCH",
    "STUCK_INITIATED",
    "ORPHAN_SETTLEMENT",
}
_SEVERITIES = {"critical", "high", "medium", "low"}


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


@router.get("/summary")
def reconciliation_summary(
    group_by: str = Query(default="merchant,date"),
    granularity: str = Query(default="day"),
    merchant_id: Optional[list[str]] = Query(default=None),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    include_totals: bool = Query(default=True),
    limit: int = Query(default=None),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    dims = [d.strip() for d in group_by.split(",") if d.strip()]
    if not dims or any(d not in _GROUP_DIMS for d in dims):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"group_by must be a subset of {sorted(_GROUP_DIMS)}"},
        )
    if granularity not in _GRANULARITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"granularity must be one of {sorted(_GRANULARITIES)}"},
        )

    limit = limit or settings.DEFAULT_SUMMARY_PAGE_SIZE
    if limit > settings.MAX_SUMMARY_PAGE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"limit must be <= {settings.MAX_SUMMARY_PAGE_SIZE}"},
        )

    from_ts = _parse_date(from_) if from_ else None
    to_ts = _parse_date(to) if to else None
    if from_ts and to_ts and from_ts > to_ts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "'from' must be before 'to'"},
        )

    rows, total = query_summary(
        db,
        group_by=dims,
        granularity=granularity,
        merchant_ids=merchant_id,
        from_ts=from_ts,
        to_ts=to_ts,
        include_totals=include_totals,
        limit=limit,
        offset=offset,
    )
    for r in rows:
        r.pop("total_groups", None)
        r.setdefault("is_total_row", False)

    return {
        "data": rows,
        "parameters": {"group_by": dims, "granularity": granularity},
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": offset + len(rows) < total,
        },
    }


@router.get("/discrepancies")
def reconciliation_discrepancies(
    type: Optional[list[str]] = Query(default=None),
    merchant_id: Optional[list[str]] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    unsettled_after_hours: int = Query(default=None),
    stuck_after_hours: int = Query(default=None),
    sort_by: str = Query(default="detected_amount"),
    limit: int = Query(default=None),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if type and any(t not in _DISCREPANCY_TYPES for t in type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"type must be a subset of {sorted(_DISCREPANCY_TYPES)}"},
        )
    if severity and severity not in _SEVERITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"severity must be one of {sorted(_SEVERITIES)}"},
        )

    unsettled_after_hours = unsettled_after_hours if unsettled_after_hours is not None else settings.UNSETTLED_AFTER_HOURS
    stuck_after_hours = stuck_after_hours if stuck_after_hours is not None else settings.STUCK_AFTER_HOURS

    limit = limit or settings.DEFAULT_PAGE_SIZE
    if limit > settings.MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": f"limit must be <= {settings.MAX_PAGE_SIZE}"},
        )

    from_ts = _parse_date(from_) if from_ else None
    to_ts = _parse_date(to) if to else None

    rows, counts_by_type, total = query_discrepancies(
        db,
        merchant_ids=merchant_id,
        types=type,
        severity=severity,
        from_ts=from_ts,
        to_ts=to_ts,
        unsettled_after_hours=unsettled_after_hours,
        stuck_after_hours=stuck_after_hours,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )

    data = [
        {
            "transaction_id": r["transaction_id"],
            "merchant_id": r["merchant_id"],
            "merchant_name": r["merchant_name"],
            "discrepancy_type": r["discrepancy_type"],
            "severity": r["severity"],
            "status": r["status"],
            "payment_amount": r["payment_amount"],
            "settlement_amount": r["settlement_amount"],
            "amount_delta": r["amount_delta"],
            "age_hours": round(r["age_hours"], 1) if r["age_hours"] is not None else None,
            "reason": r["reason"],
        }
        for r in rows
    ]

    return {
        "data": data,
        "counts_by_type": counts_by_type,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": offset + len(data) < total,
        },
        "parameters": {
            "unsettled_after_hours": unsettled_after_hours,
            "stuck_after_hours": stuck_after_hours,
        },
    }
