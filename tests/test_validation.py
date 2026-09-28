"""Validation errors, error envelope shape, and partial-success batching."""
import datetime as dt
import uuid


def _event(**overrides):
    base = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_test",
        "merchant_name": "Test Co",
        "amount": 100.0,
        "currency": "INR",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def test_negative_amount_rejected(client):
    r = client.post("/events", json=_event(amount=-5))
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "request_id" in body["error"]


def test_zero_amount_rejected(client):
    r = client.post("/events", json=_event(amount=0))
    assert r.status_code == 422


def test_bad_event_type_rejected(client):
    r = client.post("/events", json=_event(event_type="something_else"))
    assert r.status_code == 422


def test_naive_timestamp_rejected(client):
    r = client.post("/events", json=_event(timestamp="2026-01-01T00:00:00"))  # no offset
    assert r.status_code == 422


def test_future_timestamp_rejected(client):
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    r = client.post("/events", json=_event(timestamp=future.isoformat()))
    assert r.status_code == 422


def test_malformed_uuid_rejected(client):
    r = client.post("/events", json=_event(event_id="not-a-uuid"))
    assert r.status_code == 422


def test_bad_currency_rejected(client):
    r = client.post("/events", json=_event(currency="rs"))  # lowercase, invalid
    assert r.status_code == 422


def test_oversized_batch_rejected(client):
    events = [_event() for _ in range(1001)]
    r = client.post("/events", json={"events": events})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "BATCH_TOO_LARGE"


def test_unknown_transaction_returns_404_envelope(client):
    r = client.get(f"/transactions/{uuid.uuid4()}")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "TRANSACTION_NOT_FOUND"
    assert "request_id" in body["error"]


def test_malformed_transaction_id_returns_422_not_404(client):
    r = client.get("/transactions/not-a-uuid")
    assert r.status_code == 422


def test_bad_sort_by_returns_400(client):
    r = client.get("/transactions", params={"sort_by": "; DROP TABLE transactions;"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_limit_over_cap_returns_400(client):
    r = client.get("/transactions", params={"limit": 10000})
    assert r.status_code == 400


def test_from_after_to_returns_400(client):
    r = client.get("/transactions", params={"from": "2026-06-01", "to": "2026-01-01"})
    assert r.status_code == 400


def test_every_response_has_request_id_header(client):
    r = client.get("/health")
    assert "X-Request-ID" in r.headers
