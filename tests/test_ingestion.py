"""Idempotency: the assignment's most heavily-weighted requirement."""
import datetime as dt
import uuid


def _event(**overrides):
    base = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_test",
        "merchant_name": "Test Co",
        "amount": 100.50,
        "currency": "INR",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def test_single_event_ingested_once(client):
    e = _event()
    r = client.post("/events", json=e)
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 1
    assert body["duplicates"] == 0


def test_exact_replay_is_a_no_op(client):
    e = _event()
    client.post("/events", json=e)
    r2 = client.post("/events", json=e)
    body = r2.json()
    assert body["inserted"] == 0
    assert body["duplicates"] == 1

    # projection must be unchanged: event_count still 1, not 2
    tid = e["transaction_id"]
    detail = client.get(f"/transactions/{tid}").json()
    assert detail["transaction"]["event_count"] == 1


def test_intra_batch_duplicate_event_id(client):
    e = _event()
    r = client.post("/events", json={"events": [e, e, e]})
    body = r.json()
    assert body["received"] == 3
    assert body["inserted"] == 1
    assert body["duplicates"] == 2


def test_full_replay_of_loader_is_idempotent(client):
    """Simulates re-running the sample-data loader: every event posted
    twice across two full passes should yield zero net change the second
    time."""
    tid = str(uuid.uuid4())
    events = [
        _event(event_type="payment_initiated", transaction_id=tid, amount=500.00),
        _event(event_type="payment_processed", transaction_id=tid, amount=500.00),
        _event(event_type="settled", transaction_id=tid, amount=500.00),
    ]
    r1 = client.post("/events", json={"events": events})
    assert r1.json()["inserted"] == 3

    r2 = client.post("/events", json={"events": events})
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates"] == 3

    detail = client.get(f"/transactions/{tid}").json()
    assert detail["transaction"]["event_count"] == 3
    assert detail["transaction"]["status"] == "settled"


def test_concurrent_replay_of_same_event_lands_once(client, db):
    """20 'concurrent' posts of the same event_id (simulated sequentially,
    since the correctness guarantee comes from the DB-level unique
    constraint + ON CONFLICT, not from application locking) must still
    result in exactly one insert."""
    e = _event()
    results = [client.post("/events", json=e).json() for _ in range(20)]
    total_inserted = sum(r["inserted"] for r in results)
    assert total_inserted == 1


def test_out_of_order_arrival_same_projection_as_ordered(client):
    """settled arriving before payment_processed (but with correct,
    consistent timestamps) must produce the same projection as if they
    arrived in order -- ordering must come from event_timestamp, not
    arrival sequence."""
    tid = str(uuid.uuid4())
    t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    initiated = _event(event_type="payment_initiated", transaction_id=tid, timestamp=t0.isoformat())
    processed = _event(
        event_type="payment_processed", transaction_id=tid, timestamp=(t0 + dt.timedelta(hours=1)).isoformat()
    )
    settled = _event(
        event_type="settled", transaction_id=tid, timestamp=(t0 + dt.timedelta(hours=2)).isoformat()
    )

    # Post settled first, then processed, then initiated -- reverse arrival order.
    client.post("/events", json=settled)
    client.post("/events", json=processed)
    client.post("/events", json=initiated)

    detail = client.get(f"/transactions/{tid}").json()
    txn = detail["transaction"]
    assert txn["status"] == "settled"
    assert txn["initiated_at"] is not None
    assert txn["processed_at"] is not None
    assert txn["settled_at"] is not None
    assert txn["event_count"] == 3


def test_projection_rebuild_matches_incremental(client, db):
    """The projection rebuilt from the event log must be byte-identical
    (column for column) to the one built incrementally by ingestion.
    This is what licenses treating `transactions` as a derived cache
    rather than a second source of truth."""
    from sqlalchemy import text

    from scripts.rebuild_projection import rebuild

    tid1, tid2 = str(uuid.uuid4()), str(uuid.uuid4())
    events = [
        _event(event_type="payment_initiated", transaction_id=tid1, amount=250.00),
        _event(event_type="payment_processed", transaction_id=tid1, amount=250.00),
        _event(event_type="settled", transaction_id=tid1, amount=250.00),
        _event(event_type="payment_initiated", transaction_id=tid2, amount=999.99),
        _event(event_type="payment_failed", transaction_id=tid2, amount=999.99),
    ]
    client.post("/events", json={"events": events})

    before = db.execute(
        text("SELECT * FROM transactions ORDER BY transaction_id")
    ).mappings().all()
    before_snapshot = [dict(r) for r in before]

    rebuild(db)

    after = db.execute(
        text("SELECT * FROM transactions ORDER BY transaction_id")
    ).mappings().all()
    after_snapshot = [dict(r) for r in after]

    # created_at/updated_at will legitimately differ (rebuild re-inserts);
    # compare everything else column for column.
    ignore = {"created_at", "updated_at"}
    for b, a in zip(before_snapshot, after_snapshot):
        for k in b:
            if k not in ignore:
                assert b[k] == a[k], f"mismatch in {k}: {b[k]!r} != {a[k]!r}"
