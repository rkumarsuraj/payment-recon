#!/usr/bin/env python3
"""Rebuild the `transactions` projection from `payment_events` from scratch.

This is what makes the projection a derived cache rather than a second
source of truth: if it is ever wrong, this script recovers it. Run
directly, or import `rebuild(session)` in a test to assert that the
rebuilt projection matches the one built incrementally by ingestion.

Usage:
    DATABASE_URL=postgresql+psycopg2://... python scripts/rebuild_projection.py
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

_REBUILD_SQL = text(
    """
    TRUNCATE transactions;

    INSERT INTO transactions (
        transaction_id, merchant_id, currency,
        initiated_at, processed_at, failed_at, settled_at, last_settled_at,
        first_event_at, last_event_at,
        initiated_amount, processed_amount, settlement_amount,
        event_count, initiated_event_count, processed_event_count,
        failed_event_count, settled_event_count
    )
    SELECT
        transaction_id,
        (array_agg(merchant_id))[1],
        (array_agg(currency))[1],
        min(event_timestamp) FILTER (WHERE event_type = 'payment_initiated'),
        min(event_timestamp) FILTER (WHERE event_type = 'payment_processed'),
        min(event_timestamp) FILTER (WHERE event_type = 'payment_failed'),
        min(event_timestamp) FILTER (WHERE event_type = 'settled'),
        max(event_timestamp) FILTER (WHERE event_type = 'settled'),
        min(event_timestamp),
        max(event_timestamp),
        max(amount) FILTER (WHERE event_type = 'payment_initiated'),
        max(amount) FILTER (WHERE event_type = 'payment_processed'),
        max(amount) FILTER (WHERE event_type = 'settled'),
        count(*),
        count(*) FILTER (WHERE event_type = 'payment_initiated'),
        count(*) FILTER (WHERE event_type = 'payment_processed'),
        count(*) FILTER (WHERE event_type = 'payment_failed'),
        count(*) FILTER (WHERE event_type = 'settled')
    FROM payment_events
    GROUP BY transaction_id;
    """
)


def rebuild(session: Session) -> int:
    """Truncate and recompute `transactions`. Returns the row count."""
    for stmt in str(_REBUILD_SQL).split(";"):
        stmt = stmt.strip()
        if stmt:
            session.execute(text(stmt))
    session.commit()
    count = session.execute(text("SELECT count(*) FROM transactions")).scalar()
    return count


if __name__ == "__main__":
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        n = rebuild(db)
        print(f"Rebuilt projection: {n} transactions.")
    finally:
        db.close()
