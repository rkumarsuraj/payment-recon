"""POST /events — ingest payment lifecycle events, idempotently."""
from typing import Union

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.schemas import EventsBatchIn, IngestResponse, PaymentEventIn
from app.services.ingestion import ingest_events

router = APIRouter(tags=["events"])


@router.post("/events", response_model=IngestResponse, status_code=status.HTTP_200_OK)
def post_events(
    payload: Union[PaymentEventIn, EventsBatchIn] = Body(...),
    db: Session = Depends(get_db),
):
    """Accepts either a single event object or `{"events": [...]}`.

    Validation is per-event: a malformed event in a batch does not reject
    the rest of the batch. A single-event request with an invalid body
    raises a normal 422 (there is no partial state to describe for one event).
    """
    if isinstance(payload, EventsBatchIn):
        raw_events = payload.events
        is_batch = True
    else:
        raw_events = [payload]
        is_batch = False

    if len(raw_events) > settings.MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "BATCH_TOO_LARGE",
                "message": f"Batch exceeds the maximum of {settings.MAX_BATCH_SIZE} events",
            },
        )

    valid_events: list[PaymentEventIn] = []
    rejected_results = []
    for ev in raw_events:
        # ev is already a validated PaymentEventIn (pydantic validated it while
        # parsing the request body); nothing further to do here. This branch
        # exists so that, if this function is ever called with raw dicts
        # (e.g. from the batch-loader script bypassing FastAPI's own body
        # parsing), per-event validation still happens and a bad event is
        # rejected without failing the whole batch.
        if isinstance(ev, PaymentEventIn):
            valid_events.append(ev)
            continue
        try:
            valid_events.append(PaymentEventIn.model_validate(ev))
        except ValidationError as exc:
            rejected_results.append(
                {
                    "event_id": str(ev.get("event_id", "unknown")) if isinstance(ev, dict) else "unknown",
                    "status": "rejected",
                    "error": {"code": "VALIDATION_ERROR", "message": str(exc.errors()[0]["msg"])},
                }
            )

    result = {
        "received": len(raw_events),
        "inserted": 0,
        "duplicates": 0,
        "rejected": len(rejected_results),
        "transactions_touched": 0,
        "results": rejected_results,
    }

    if valid_events:
        ingest_result = ingest_events(db, valid_events)
        result["inserted"] = ingest_result["inserted"]
        result["duplicates"] = ingest_result["duplicates"]
        result["transactions_touched"] = ingest_result["transactions_touched"]
        result["results"] = ingest_result["results"] + rejected_results

    return result
