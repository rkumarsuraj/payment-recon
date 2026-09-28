"""Idempotent event ingestion.

Three layers of idempotency (see TECH_SPEC.md §5):
  1. `event_id` PRIMARY KEY on payment_events + `ON CONFLICT DO NOTHING`.
  2. The transactions projection is derived only from rows that the INSERT
     above actually returned — so a duplicate event changes nothing.
  3. Logical duplicates that arrive under a *new* event_id (e.g. the same
     settlement re-announced) cannot be suppressed here; they are left to
     be *detected* by the discrepancy endpoint (DOUBLE_SETTLEMENT).

Both statements below run inside one DB transaction per batch, so there is
never a window where an event exists in the log but not in the projection.
"""
import datetime as dt
import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas import PaymentEventIn

# One INSERT ... SELECT * FROM unnest(...) ON CONFLICT DO NOTHING RETURNING ...
_INSERT_EVENTS_SQL = text(
    """
    WITH inserted AS (
        INSERT INTO payment_events
            (event_id, transaction_id, merchant_id, event_type, amount,
             currency, event_timestamp, raw_payload)
        SELECT * FROM unnest(
            :event_ids ::uuid[],
            :txn_ids ::uuid[],
            :merchant_ids ::text[],
            :types ::event_type_enum[],
            :amounts ::numeric[],
            :currencies ::char(3)[],
            :timestamps ::timestamptz[],
            :payloads ::jsonb[]
        )
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id, transaction_id, merchant_id, event_type,
                  amount, currency, event_timestamp
    )
    SELECT * FROM inserted;
    """
)

# Upsert merchants (last-writer-wins on display name; safe, descriptive only)
_UPSERT_MERCHANTS_SQL = text(
    """
    INSERT INTO merchants (merchant_id, merchant_name, created_at, updated_at)
    SELECT merchant_id, merchant_name, now(), now()
    FROM unnest(:merchant_ids ::text[], :merchant_names ::text[]) AS m(merchant_id, merchant_name)
    ON CONFLICT (merchant_id) DO UPDATE
        SET merchant_name = EXCLUDED.merchant_name, updated_at = now();
    """
)

_PROJECT_SQL = text(
    """
    WITH rolled AS (
        SELECT
            transaction_id, merchant_id, currency,
            min(event_timestamp)                                             AS first_event_at,
            max(event_timestamp)                                             AS last_event_at,
            min(event_timestamp) FILTER (WHERE event_type = 'payment_initiated') AS initiated_at,
            min(event_timestamp) FILTER (WHERE event_type = 'payment_processed') AS processed_at,
            min(event_timestamp) FILTER (WHERE event_type = 'payment_failed')    AS failed_at,
            min(event_timestamp) FILTER (WHERE event_type = 'settled')           AS settled_at,
            max(event_timestamp) FILTER (WHERE event_type = 'settled')           AS last_settled_at,
            max(amount) FILTER (WHERE event_type = 'payment_initiated')          AS initiated_amount,
            max(amount) FILTER (WHERE event_type = 'payment_processed')          AS processed_amount,
            max(amount) FILTER (WHERE event_type = 'settled')                    AS settlement_amount,
            count(*)                                                           AS event_count,
            count(*) FILTER (WHERE event_type = 'payment_initiated')            AS initiated_event_count,
            count(*) FILTER (WHERE event_type = 'payment_processed')            AS processed_event_count,
            count(*) FILTER (WHERE event_type = 'payment_failed')               AS failed_event_count,
            count(*) FILTER (WHERE event_type = 'settled')                      AS settled_event_count
        FROM unnest(
            :ins_txn_ids ::uuid[], :ins_merchant_ids ::text[], :ins_currencies ::char(3)[],
            :ins_types ::event_type_enum[], :ins_amounts ::numeric[], :ins_timestamps ::timestamptz[]
        ) AS e(transaction_id, merchant_id, currency, event_type, amount, event_timestamp)
        GROUP BY transaction_id, merchant_id, currency
    )
    INSERT INTO transactions AS t (
        transaction_id, merchant_id, currency,
        initiated_at, processed_at, failed_at, settled_at, last_settled_at,
        first_event_at, last_event_at,
        initiated_amount, processed_amount, settlement_amount,
        event_count, initiated_event_count, processed_event_count,
        failed_event_count, settled_event_count
    )
    SELECT
        transaction_id, merchant_id, currency,
        initiated_at, processed_at, failed_at, settled_at, last_settled_at,
        first_event_at, last_event_at,
        initiated_amount, processed_amount, settlement_amount,
        event_count, initiated_event_count, processed_event_count,
        failed_event_count, settled_event_count
    FROM rolled
    ON CONFLICT (transaction_id) DO UPDATE SET
        initiated_at      = LEAST(t.initiated_at,  EXCLUDED.initiated_at),
        processed_at      = LEAST(t.processed_at,  EXCLUDED.processed_at),
        failed_at         = LEAST(t.failed_at,     EXCLUDED.failed_at),
        settled_at        = LEAST(t.settled_at,    EXCLUDED.settled_at),
        last_settled_at   = GREATEST(t.last_settled_at, EXCLUDED.last_settled_at),
        first_event_at    = LEAST(t.first_event_at, EXCLUDED.first_event_at),
        last_event_at     = GREATEST(t.last_event_at, EXCLUDED.last_event_at),
        initiated_amount  = COALESCE(EXCLUDED.initiated_amount,  t.initiated_amount),
        processed_amount  = COALESCE(EXCLUDED.processed_amount,  t.processed_amount),
        settlement_amount = COALESCE(EXCLUDED.settlement_amount, t.settlement_amount),
        event_count            = t.event_count            + EXCLUDED.event_count,
        initiated_event_count  = t.initiated_event_count  + EXCLUDED.initiated_event_count,
        processed_event_count  = t.processed_event_count  + EXCLUDED.processed_event_count,
        failed_event_count     = t.failed_event_count     + EXCLUDED.failed_event_count,
        settled_event_count    = t.settled_event_count    + EXCLUDED.settled_event_count,
        updated_at = now()
    RETURNING transaction_id;
    """
)


def _dt_to_str(v: dt.datetime) -> str:
    return v.isoformat()


def ingest_events(db: Session, events: list[PaymentEventIn]) -> dict:
    """Ingest a batch of already-Pydantic-validated events.

    Returns a dict with received/inserted/duplicates/rejected counts,
    transactions_touched, and a per-event result list. Validation failures
    are handled by the caller (router) before this function is called;
    this function only handles intra-batch duplicate event_ids.
    """
    # De-duplicate within the batch itself: ON CONFLICT cannot fire twice
    # for the same key inside one statement, so if the same event_id shows
    # up twice in one request body we keep the first occurrence and report
    # the rest as duplicates up front.
    seen: dict[str, PaymentEventIn] = {}
    intra_batch_duplicates: list[str] = []
    for ev in events:
        key = str(ev.event_id)
        if key in seen:
            intra_batch_duplicates.append(key)
        else:
            seen[key] = ev

    unique_events = list(seen.values())

    # Upsert merchants first (payment_events.merchant_id has an FK to merchants).
    merchant_map: dict[str, str] = {}
    for ev in unique_events:
        if ev.merchant_id not in merchant_map and ev.merchant_name:
            merchant_map[ev.merchant_id] = ev.merchant_name
    for ev in unique_events:
        merchant_map.setdefault(ev.merchant_id, ev.merchant_id)  # fallback name

    if merchant_map:
        db.execute(
            _UPSERT_MERCHANTS_SQL,
            {
                "merchant_ids": list(merchant_map.keys()),
                "merchant_names": list(merchant_map.values()),
            },
        )

    params = {
        "event_ids": [str(e.event_id) for e in unique_events],
        "txn_ids": [str(e.transaction_id) for e in unique_events],
        "merchant_ids": [e.merchant_id for e in unique_events],
        "types": [e.event_type.value for e in unique_events],
        "amounts": [str(e.amount) for e in unique_events],
        "currencies": [e.currency for e in unique_events],
        "timestamps": [_dt_to_str(e.timestamp) for e in unique_events],
        "payloads": [json.dumps(_event_payload(e)) for e in unique_events],
    }

    inserted_rows = db.execute(_INSERT_EVENTS_SQL, params).mappings().all()
    inserted_ids = {str(r["event_id"]) for r in inserted_rows}

    transactions_touched = 0
    if inserted_rows:
        proj_params = {
            "ins_txn_ids": [str(r["transaction_id"]) for r in inserted_rows],
            "ins_merchant_ids": [r["merchant_id"] for r in inserted_rows],
            "ins_currencies": [r["currency"] for r in inserted_rows],
            "ins_types": [r["event_type"] for r in inserted_rows],
            "ins_amounts": [str(r["amount"]) for r in inserted_rows],
            "ins_timestamps": [_dt_to_str(r["event_timestamp"]) for r in inserted_rows],
        }
        touched = db.execute(_PROJECT_SQL, proj_params).fetchall()
        transactions_touched = len(touched)

    db.commit()

    results = []
    for ev in unique_events:
        key = str(ev.event_id)
        if key in inserted_ids:
            results.append({"event_id": key, "status": "inserted"})
        else:
            results.append(
                {
                    "event_id": key,
                    "status": "duplicate",
                    "message": "event_id already ingested; no state change",
                }
            )
    for key in intra_batch_duplicates:
        results.append(
            {
                "event_id": key,
                "status": "duplicate",
                "message": "duplicate event_id within this request; only the first occurrence was considered",
            }
        )

    return {
        "received": len(events),
        "inserted": len(inserted_ids),
        "duplicates": len(unique_events) - len(inserted_ids) + len(intra_batch_duplicates),
        "rejected": 0,
        "transactions_touched": transactions_touched,
        "results": results,
    }


def _event_payload(ev: PaymentEventIn) -> dict:
    return {
        "event_id": str(ev.event_id),
        "event_type": ev.event_type.value,
        "transaction_id": str(ev.transaction_id),
        "merchant_id": ev.merchant_id,
        "merchant_name": ev.merchant_name,
        "amount": str(ev.amount),
        "currency": ev.currency,
        "timestamp": ev.timestamp.isoformat(),
    }
