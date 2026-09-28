"""GET /transactions filters/sort/pagination, GET /reconciliation/summary."""
import datetime as dt
import uuid

NOW = dt.datetime.now(dt.timezone.utc)


def _post(client, event_type, tid, amount=100.0, hours_ago=1, merchant="merchant_a"):
    ts = NOW - dt.timedelta(hours=hours_ago)
    client.post(
        "/events",
        json={
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "transaction_id": tid,
            "merchant_id": merchant,
            "merchant_name": merchant.title(),
            "amount": amount,
            "currency": "INR",
            "timestamp": ts.isoformat(),
        },
    )


def _make_transaction(client, merchant, status_events, hours_ago_start=10):
    """status_events: list of (event_type, amount, hours_ago)."""
    tid = str(uuid.uuid4())
    for event_type, amount, hours_ago in status_events:
        _post(client, event_type, tid, amount=amount, hours_ago=hours_ago, merchant=merchant)
    return tid


def test_filter_by_merchant(client):
    tid_a = _make_transaction(client, "merchant_a", [("payment_initiated", 100, 5)])
    tid_b = _make_transaction(client, "merchant_b", [("payment_initiated", 100, 5)])

    r = client.get("/transactions", params={"merchant_id": "merchant_a"})
    ids = {t["transaction_id"] for t in r.json()["data"]}
    assert tid_a in ids
    assert tid_b not in ids


def test_filter_by_status(client):
    tid_settled = _make_transaction(
        client, "merchant_a", [("payment_initiated", 100, 3), ("payment_processed", 100, 2), ("settled", 100, 1)]
    )
    tid_initiated = _make_transaction(client, "merchant_a", [("payment_initiated", 100, 1)])

    r = client.get("/transactions", params={"status": "settled"})
    ids = {t["transaction_id"] for t in r.json()["data"]}
    assert tid_settled in ids
    assert tid_initiated not in ids


def test_filter_by_date_range(client):
    tid_old = _make_transaction(client, "merchant_a", [("payment_initiated", 100, 200)])
    tid_recent = _make_transaction(client, "merchant_a", [("payment_initiated", 100, 1)])

    cutoff = (NOW - dt.timedelta(hours=24)).date().isoformat()
    r = client.get("/transactions", params={"from": cutoff})
    ids = {t["transaction_id"] for t in r.json()["data"]}
    assert tid_recent in ids
    assert tid_old not in ids


def test_pagination_no_gaps_or_overlap(client):
    tids = [_make_transaction(client, "merchant_a", [("payment_initiated", 100, i + 1)]) for i in range(10)]

    page1 = client.get("/transactions", params={"limit": 4, "offset": 0, "merchant_id": "merchant_a"}).json()
    page2 = client.get("/transactions", params={"limit": 4, "offset": 4, "merchant_id": "merchant_a"}).json()
    page3 = client.get("/transactions", params={"limit": 4, "offset": 8, "merchant_id": "merchant_a"}).json()

    all_ids = (
        [t["transaction_id"] for t in page1["data"]]
        + [t["transaction_id"] for t in page2["data"]]
        + [t["transaction_id"] for t in page3["data"]]
    )
    assert len(all_ids) == len(set(all_ids)) == 10  # no duplicate, no missing rows across pages
    assert page1["pagination"]["total"] == 10


def test_sort_by_amount(client):
    _make_transaction(client, "merchant_a", [("payment_initiated", 50, 1)])
    _make_transaction(client, "merchant_a", [("payment_initiated", 500, 1)])
    _make_transaction(client, "merchant_a", [("payment_initiated", 200, 1)])

    r = client.get(
        "/transactions", params={"merchant_id": "merchant_a", "sort_by": "payment_amount", "sort_order": "desc"}
    )
    amounts = [float(t["payment_amount"]) for t in r.json()["data"]]
    assert amounts == sorted(amounts, reverse=True)


def test_has_discrepancy_filter(client):
    tid_clean = _make_transaction(
        client, "merchant_a", [("payment_initiated", 100, 3), ("payment_processed", 100, 2), ("settled", 100, 1)]
    )
    tid_stuck = _make_transaction(client, "merchant_a", [("payment_initiated", 100, 30)])

    r = client.get("/transactions", params={"merchant_id": "merchant_a", "has_discrepancy": True})
    ids = {t["transaction_id"] for t in r.json()["data"]}
    assert tid_stuck in ids
    assert tid_clean not in ids


def test_summary_group_by_merchant(client):
    _make_transaction(client, "merchant_a", [("payment_initiated", 100, 1), ("payment_processed", 100, 1)])
    _make_transaction(client, "merchant_b", [("payment_initiated", 200, 1)])

    r = client.get("/reconciliation/summary", params={"group_by": "merchant", "include_totals": False})
    body = r.json()
    merchants = {row["merchant_id"] for row in body["data"]}
    assert {"merchant_a", "merchant_b"} <= merchants


def test_summary_totals_row_matches_sum_of_groups(client):
    _make_transaction(client, "merchant_a", [("payment_initiated", 300, 1)])
    _make_transaction(client, "merchant_b", [("payment_initiated", 700, 1)])

    r = client.get("/reconciliation/summary", params={"group_by": "merchant", "include_totals": True, "limit": 500})
    rows = r.json()["data"]
    group_rows = [row for row in rows if not row["is_total_row"]]
    total_row = next(row for row in rows if row["is_total_row"])

    assert sum(g["transaction_count"] for g in group_rows) == total_row["transaction_count"]


def test_summary_group_by_status(client):
    _make_transaction(client, "merchant_a", [("payment_initiated", 100, 1)])
    _make_transaction(
        client, "merchant_a", [("payment_initiated", 100, 3), ("payment_processed", 100, 2), ("settled", 100, 1)]
    )

    r = client.get("/reconciliation/summary", params={"group_by": "status", "include_totals": False})
    statuses = {row["status"] for row in r.json()["data"]}
    assert "initiated" in statuses
    assert "settled" in statuses
