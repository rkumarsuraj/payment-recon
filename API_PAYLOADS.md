# API Request Payload Reference

Every request shape actually accepted by the service, for each of the
five endpoints. All examples below use realistic values from the sample
dataset (merchants `merchant_1`–`merchant_5`, INR amounts) and were
validated against the running service, not written from memory.

Base URL: `http://localhost:8000` locally, or your Render URL once deployed.

---

## 1. `POST /events`

### 1a. Single event

```http
POST /events
Content-Type: application/json
```

```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```

**Field notes:**
- `event_id` — UUID, must be unique per event (this is the idempotency key)
- `event_type` — one of `payment_initiated`, `payment_processed`, `payment_failed`, `settled`
- `transaction_id` — UUID, shared across all events belonging to the same transaction
- `merchant_id` — free-text identifier, 1–128 chars
- `merchant_name` — optional; if omitted, falls back to whatever name is already on file for that merchant
- `amount` — decimal > 0, up to 2 decimal places
- `currency` — exactly 3 uppercase letters (e.g. `INR`)
- `timestamp` — **must be timezone-aware** (include a UTC offset) and not more than 5 minutes in the future

### 1b. Batch (up to 1,000 events)

```http
POST /events
Content-Type: application/json
```

```json
{
  "events": [
    {
      "event_id": "aaaaaaaa-0000-0000-0000-000000000001",
      "event_type": "payment_initiated",
      "transaction_id": "11111111-0000-0000-0000-000000000001",
      "merchant_id": "merchant_1",
      "merchant_name": "QuickMart",
      "amount": 1500.00,
      "currency": "INR",
      "timestamp": "2026-03-01T09:00:00+00:00"
    },
    {
      "event_id": "aaaaaaaa-0000-0000-0000-000000000002",
      "event_type": "payment_processed",
      "transaction_id": "11111111-0000-0000-0000-000000000001",
      "merchant_id": "merchant_1",
      "merchant_name": "QuickMart",
      "amount": 1500.00,
      "currency": "INR",
      "timestamp": "2026-03-01T09:01:30+00:00"
    },
    {
      "event_id": "aaaaaaaa-0000-0000-0000-000000000003",
      "event_type": "settled",
      "transaction_id": "11111111-0000-0000-0000-000000000001",
      "merchant_id": "merchant_1",
      "merchant_name": "QuickMart",
      "amount": 1500.00,
      "currency": "INR",
      "timestamp": "2026-03-03T04:15:00+00:00"
    }
  ]
}
```

### 1c. Replaying the exact same event (idempotency proof)

Post 1a's payload again, unchanged. The response `inserted` will be `0`
and `duplicates` will be `1` — no new row, no state change.

### 1d. A batch with one intentionally invalid event (partial-success proof)

```json
{
  "events": [
    {
      "event_id": "bbbbbbbb-0000-0000-0000-000000000001",
      "event_type": "payment_initiated",
      "transaction_id": "22222222-0000-0000-0000-000000000001",
      "merchant_id": "merchant_3",
      "merchant_name": "UrbanEats",
      "amount": 800.00,
      "currency": "INR",
      "timestamp": "2026-03-05T10:00:00+00:00"
    },
    {
      "event_id": "bbbbbbbb-0000-0000-0000-000000000002",
      "event_type": "payment_initiated",
      "transaction_id": "22222222-0000-0000-0000-000000000002",
      "merchant_id": "merchant_3",
      "merchant_name": "UrbanEats",
      "amount": -50.00,
      "currency": "INR",
      "timestamp": "2026-03-05T10:01:00+00:00"
    }
  ]
}
```

The first event is accepted (`inserted: 1`); the second is rejected
(`rejected: 1`, negative amount) without affecting the first.

---

## 2. `GET /transactions`

No request body — everything is query parameters, all optional.

```
GET /transactions?limit=10
```

```
GET /transactions?merchant_id=merchant_1&status=settled&limit=20
```

```
GET /transactions?merchant_id=merchant_1&merchant_id=merchant_2&status=processed&status=failed
```
*(repeat a param to pass multiple values — this returns transactions from either merchant, in either status)*

```
GET /transactions?from=2026-02-01&to=2026-03-01&date_field=initiated_at
```

```
GET /transactions?min_amount=1000&max_amount=50000&sort_by=payment_amount&sort_order=desc
```

```
GET /transactions?has_discrepancy=true&limit=50
```

```
GET /transactions?payment_status=processed&settlement_status=pending
```

```
GET /transactions?limit=20&offset=40
```
*(page 3, if page size is 20)*

**All available params:** `merchant_id` (repeatable), `status` (repeatable),
`payment_status`, `settlement_status`, `from`, `to`, `date_field`
(`initiated_at`|`last_event_at`|`settled_at`, default `initiated_at`),
`min_amount`, `max_amount`, `has_discrepancy` (true/false), `sort_by`
(`initiated_at`|`last_event_at`|`payment_amount`|`status`), `sort_order`
(`asc`|`desc`), `limit` (max 200, default 50), `offset`.

---

## 3. `GET /transactions/{transaction_id}`

Path parameter only, no query string or body.

```
GET /transactions/1c3196f6-801d-425f-aca9-df47ec7a394a
```

Unknown ID (still a valid UUID, just doesn't exist):

```
GET /transactions/00000000-0000-0000-0000-000000000000
```
→ `404` with the standard error envelope.

Malformed ID:

```
GET /transactions/not-a-uuid
```
→ `422` (invalid UUID, distinct from 404).

---

## 4. `GET /reconciliation/summary`

No request body — query parameters only.

```
GET /reconciliation/summary?group_by=merchant
```

```
GET /reconciliation/summary?group_by=merchant,date
```

```
GET /reconciliation/summary?group_by=merchant,date,status&granularity=day
```

```
GET /reconciliation/summary?group_by=date&granularity=month&merchant_id=merchant_1
```

```
GET /reconciliation/summary?group_by=status&from=2026-01-01&to=2026-04-01&include_totals=true
```

```
GET /reconciliation/summary?group_by=merchant&include_totals=false&limit=100&offset=0
```

**All available params:** `group_by` (CSV of `merchant`, `date`, `status`
— default `merchant,date`), `granularity` (`day`|`week`|`month`, default
`day`), `merchant_id` (repeatable), `from`, `to`, `include_totals`
(default `true`), `limit` (max 500, default 100), `offset`.

---

## 5. `GET /reconciliation/discrepancies`

No request body — query parameters only.

```
GET /reconciliation/discrepancies
```
*(all six types, default 48h/24h grace windows)*

```
GET /reconciliation/discrepancies?type=AMOUNT_MISMATCH
```

```
GET /reconciliation/discrepancies?type=DOUBLE_SETTLEMENT&type=SETTLED_AFTER_FAILURE
```
*(repeatable — either type)*

```
GET /reconciliation/discrepancies?merchant_id=merchant_1&severity=critical
```

```
GET /reconciliation/discrepancies?unsettled_after_hours=24&stuck_after_hours=12
```
*(tighter grace windows than the defaults)*

```
GET /reconciliation/discrepancies?from=2026-03-01&to=2026-04-01&sort_by=age&limit=50
```

**All available params:** `type` (repeatable, one of
`PROCESSED_NOT_SETTLED`, `SETTLED_AFTER_FAILURE`, `DOUBLE_SETTLEMENT`,
`AMOUNT_MISMATCH`, `STUCK_INITIATED`, `ORPHAN_SETTLEMENT`), `merchant_id`
(repeatable), `severity` (`critical`|`high`|`medium`|`low`), `from`, `to`,
`unsettled_after_hours` (default 48), `stuck_after_hours` (default 24),
`sort_by` (`detected_amount`|`age`|`merchant`|`type`, default
`detected_amount`), `limit` (max 200, default 50), `offset`.

---

## 6. `GET /health`

No parameters, no body.

```
GET /health
```

---

## Quick reference: which endpoints take a body vs. query params

| Endpoint | Body? | Query params? |
|---|---|---|
| `POST /events` | ✅ JSON body (single event or `{"events": [...]}`) | ❌ |
| `GET /transactions` | ❌ | ✅ |
| `GET /transactions/{id}` | ❌ | ❌ (path param only) |
| `GET /reconciliation/summary` | ❌ | ✅ |
| `GET /reconciliation/discrepancies` | ❌ | ✅ |
| `GET /health` | ❌ | ❌ |

Only `POST /events` takes a request body — every read endpoint is
filtered entirely through the URL, which is also why they're all easy to
test directly in a browser or via `curl -s "<url>?param=value"` without
needing a JSON body at all.
