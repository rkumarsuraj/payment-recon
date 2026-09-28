"""One fixture per discrepancy class, asserting it is flagged and the
others are not (and vice versa: clean transactions produce zero breaks)."""
import datetime as dt
import uuid

NOW = dt.datetime.now(dt.timezone.utc)


def _post(client, event_type, tid, amount=100.0, hours_ago=0, merchant="merchant_test"):
    ts = NOW - dt.timedelta(hours=hours_ago)
    return client.post(
        "/events",
        json={
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "transaction_id": tid,
            "merchant_id": merchant,
            "merchant_name": "Test Co",
            "amount": amount,
            "currency": "INR",
            "timestamp": ts.isoformat(),
        },
    )


def _discrepancy_types(client, tid) -> set[str]:
    r = client.get("/reconciliation/discrepancies", params={"limit": 200})
    return {d["discrepancy_type"] for d in r.json()["data"] if d["transaction_id"] == tid}


def test_clean_settled_transaction_has_no_discrepancy(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=3)
    _post(client, "payment_processed", tid, hours_ago=2)
    _post(client, "settled", tid, hours_ago=1)
    assert _discrepancy_types(client, tid) == set()


def test_clean_failed_transaction_has_no_discrepancy(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=2)
    _post(client, "payment_failed", tid, hours_ago=1)
    assert _discrepancy_types(client, tid) == set()


def test_processed_not_settled(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=72)
    _post(client, "payment_processed", tid, hours_ago=70)  # > 48h grace window
    assert _discrepancy_types(client, tid) == {"PROCESSED_NOT_SETTLED"}


def test_processed_not_settled_grace_window_boundary(client):
    """47h since processed: still in flight, must NOT be flagged.
    49h since processed: past the 48h default grace window, must be flagged."""
    tid_ok = str(uuid.uuid4())
    _post(client, "payment_initiated", tid_ok, hours_ago=50)
    _post(client, "payment_processed", tid_ok, hours_ago=47)
    assert _discrepancy_types(client, tid_ok) == set()

    tid_break = str(uuid.uuid4())
    _post(client, "payment_initiated", tid_break, hours_ago=52)
    _post(client, "payment_processed", tid_break, hours_ago=49)
    assert _discrepancy_types(client, tid_break) == {"PROCESSED_NOT_SETTLED"}


def test_settled_after_failure(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=3)
    _post(client, "payment_failed", tid, hours_ago=2)
    _post(client, "settled", tid, hours_ago=1)
    assert "SETTLED_AFTER_FAILURE" in _discrepancy_types(client, tid)


def test_double_settlement(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=3)
    _post(client, "payment_processed", tid, hours_ago=2)
    _post(client, "settled", tid, hours_ago=1)
    _post(client, "settled", tid, hours_ago=1)  # distinct event_id, same logical settlement
    assert "DOUBLE_SETTLEMENT" in _discrepancy_types(client, tid)


def test_amount_mismatch(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, amount=1000.00, hours_ago=3)
    _post(client, "payment_processed", tid, amount=1000.00, hours_ago=2)
    _post(client, "settled", tid, amount=950.00, hours_ago=1)  # short settlement
    assert "AMOUNT_MISMATCH" in _discrepancy_types(client, tid)


def test_stuck_initiated(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=30)  # > 24h default stuck window
    assert _discrepancy_types(client, tid) == {"STUCK_INITIATED"}


def test_stuck_initiated_not_flagged_when_recent(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=1)  # well within grace
    assert _discrepancy_types(client, tid) == set()


def test_orphan_settlement(client):
    tid = str(uuid.uuid4())
    _post(client, "settled", tid, hours_ago=1)  # no prior initiated/processed
    assert _discrepancy_types(client, tid) == {"ORPHAN_SETTLEMENT"}


def test_discrepancy_type_filter(client):
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=30)
    r = client.get("/reconciliation/discrepancies", params={"type": "AMOUNT_MISMATCH"})
    assert all(d["discrepancy_type"] == "AMOUNT_MISMATCH" for d in r.json()["data"])
    ids = {d["transaction_id"] for d in r.json()["data"]}
    assert tid not in ids  # this transaction is STUCK_INITIATED, not AMOUNT_MISMATCH


def test_counts_by_type_present_even_when_page_filtered(client):
    tid1, tid2 = str(uuid.uuid4()), str(uuid.uuid4())
    _post(client, "payment_initiated", tid1, hours_ago=30)  # STUCK_INITIATED
    _post(client, "settled", tid2, hours_ago=1)  # ORPHAN_SETTLEMENT

    r = client.get("/reconciliation/discrepancies", params={"type": "STUCK_INITIATED"})
    body = r.json()
    assert body["counts_by_type"].get("STUCK_INITIATED", 0) >= 1
    assert body["counts_by_type"].get("ORPHAN_SETTLEMENT", 0) >= 1  # counts ignore the type filter


def test_transaction_detail_matches_discrepancies_endpoint(client):
    """The detail endpoint and the discrepancies endpoint must never
    disagree -- they are backed by the exact same SQL rules."""
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, amount=500, hours_ago=3)
    _post(client, "payment_processed", tid, amount=500, hours_ago=2)
    _post(client, "settled", tid, amount=480, hours_ago=1)

    detail = client.get(f"/transactions/{tid}").json()
    detail_types = {d["type"] for d in detail["reconciliation"]["discrepancies"]}
    endpoint_types = _discrepancy_types(client, tid)
    assert detail_types == endpoint_types == {"AMOUNT_MISMATCH"}


def test_reason_string_reports_total_elapsed_hours_not_hour_of_day(client):
    """Regression test: `extract(hour from interval)` returns only the
    0-23 hour-of-day component of an interval, not the total elapsed
    hours. A transaction stuck for ~30 days must report roughly 720h in
    its reason string, not a value that wrapped back into 0-23."""
    tid = str(uuid.uuid4())
    _post(client, "payment_initiated", tid, hours_ago=30 * 24)  # ~30 days ago

    r = client.get("/reconciliation/discrepancies", params={"type": "STUCK_INITIATED", "limit": 200})
    row = next(d for d in r.json()["data"] if d["transaction_id"] == tid)
    assert "STUCK_INITIATED" in row["discrepancy_type"]

    detail = client.get(f"/transactions/{tid}").json()
    reason = next(
        d["reason"] for d in detail["reconciliation"]["discrepancies"] if d["type"] == "STUCK_INITIATED"
    )
    # ~720 hours; must not have wrapped into a small hour-of-day value.
    reported_hours = int("".join(ch for ch in reason if ch.isdigit()))
    assert reported_hours > 100, f"reason string reports too few hours, likely wrapped: {reason!r}"
