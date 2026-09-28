# Setu Assignment — Partner Reconciliation Service

A backend service for a partner integrating with Setu: ingests payment
lifecycle events, maintains transaction and reconciliation state, and
exposes APIs for operations teams to filter transactions, view
reconciliation summaries, and find discrepancies between payment and
settlement status.

Built with **FastAPI + SQLAlchemy + PostgreSQL 16**, using **Docker Compose**
as the primary way to run it locally (one command brings up both Postgres
and the API).

**Public deployment:** `<PASTE_RENDER_URL_HERE>` — see [Deployment](#deployment)
for the cold-start note before you hit it.

---

## Contents

1. [Architecture overview](#1-architecture-overview)
2. [Local setup](#2-local-setup)
3. [Data model](#3-data-model)
4. [API documentation](#4-api-documentation)
5. [Idempotency](#5-idempotency)
6. [Discrepancy classes](#6-discrepancy-classes)
7. [Sample data](#7-sample-data)
8. [Deployment](#8-deployment)
9. [Testing](#9-testing)
10. [Performance](#10-performance)
11. [Assumptions and tradeoffs](#11-assumptions-and-tradeoffs)
12. [What I'd do differently with more time](#12-what-id-do-differently-with-more-time)


---

## 1. Architecture overview

```
                   ┌────────────────────────────┐
  upstream systems │  POST /events (single|batch)│
  (PSP, settlement)└──────────────┬─────────────┘
                                  │  one DB transaction per batch
                                  ▼
  ┌───────────────────────────────────────────────────────────┐
  │ FastAPI app (uvicorn)                                      │
  │   app/api/       routers — events, transactions, recon.    │
  │   app/services/  ingestion + reconciliation SQL lives here │
  │   app/core/      settings, error envelope, logging, db     │
  └───────────────────────────┬───────────────────────────────┘
                              ▼
  ┌───────────────────────────────────────────────────────────┐
  │ PostgreSQL 16                                              │
  │   merchants        dimension                               │
  │   payment_events   append-only log  ← system of record     │
  │   transactions     current-state projection ← read path    │
  └───────────────────────────────────────────────────────────┘
```

### The central design decision: log + projection

`payment_events` is append-only and never updated — it is the system of
record. `transactions` is a materialized projection of it: one row per
transaction, carrying current state as of the last event processed.

**Why not compute state from the event log on every read?** The listing,
detail and reconciliation endpoints would each need a `GROUP BY` pass over
the whole event table before they could even apply a filter, so no index
could serve `status = 'settled'` or a date range — every request would
degrade linearly with total event volume. Projecting on write costs one
extra upsert per event and makes every read a single-table, index-served
query.

**Why not drop the log and only keep current state?** Then
`GET /transactions/{id}` cannot return event history, duplicate detection
has nothing to check against, and any projection bug becomes unrecoverable.

**The projection is derived, never authoritative.**
`scripts/rebuild_projection.py` truncates `transactions` and recomputes it
from `payment_events` in one SQL statement. `tests/test_ingestion.py::
test_projection_rebuild_matches_incremental` asserts the rebuilt projection
is identical, column for column, to the one built incrementally by
ingestion. That test is what licenses treating the denormalisation as safe
rather than as a shortcut.

### Project layout

```
app/
  main.py                 FastAPI app, router wiring, /health
  core/
    config.py             pydantic-settings: DATABASE_URL, grace windows, page caps
    db.py                 engine, session dependency, pool config
    errors.py             error envelope + exception handlers
    logging.py            JSON logs
  models.py                SQLAlchemy table definitions (mirrors the migration)
  schemas.py               Pydantic v2 request/response models
  api/
    events.py              POST /events
    transactions.py         GET /transactions, GET /transactions/{id}
    reconciliation.py       GET /reconciliation/summary, /discrepancies
  services/
    ingestion.py            idempotent batch insert + projection upsert
    reconciliation.py       discrepancy detection (UNION ALL of 6 classes) + summary
migrations/                alembic, one revision holding the full DDL below
scripts/
  augment_sample_data.py    injects break classes the raw sample file lacks
  load_sample_data.py       posts sample data through POST /events, batched
  rebuild_projection.py     recompute transactions from payment_events
tests/                      48 pytest tests against real Postgres
postman/                    collection + environment
docker-compose.yml          postgres + app, one command to run locally
Dockerfile
render.yaml                 deployment blueprint
```

Alembic (not `Base.metadata.create_all()`) because the schema needs
generated columns, `CHECK` constraints and partial indexes that
SQLAlchemy's table-creation would not emit — the DDL is written as
explicit, reviewable SQL in `migrations/versions/0001_initial.py`.

---

## 2. Local setup

### Option A — Docker Compose (recommended, one command)

```bash
docker compose up --build
```

This starts Postgres, waits for its healthcheck, then starts the API,
which runs `alembic upgrade head` on boot. The API is at
`http://localhost:8000`, interactive docs at `http://localhost:8000/docs`.

Load the sample data (separate terminal, once the API is up):

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python scripts/load_sample_data.py --base-url http://localhost:8000 \
    --file sample_events_augmented.json --batch-size 500
```

### Option B — no Docker

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Point at any Postgres 16 instance you have running:
export DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/setu_db
createdb setu_db   # if it doesn't already exist

alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Then load data as in Option A.

### Regenerating the augmented sample file

`sample_events_augmented.json` ships in the repo already built, but to
regenerate it from the raw `sample_events.json`:

```bash
python scripts/augment_sample_data.py \
    --input sample_events.json --output sample_events_augmented.json
```

---

## 3. Data model

```sql
CREATE TABLE merchants (
    merchant_id   TEXT PRIMARY KEY,
    merchant_name TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TYPE event_type_enum AS ENUM
    ('payment_initiated', 'payment_processed', 'payment_failed', 'settled');

CREATE TABLE payment_events (            -- append-only log, system of record
    event_id        UUID PRIMARY KEY,     -- producer-supplied; the idempotency key
    transaction_id  UUID NOT NULL,
    merchant_id     TEXT NOT NULL REFERENCES merchants(merchant_id),
    event_type      event_type_enum NOT NULL,
    amount          NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    currency        CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    event_timestamp TIMESTAMPTZ NOT NULL, -- when it happened
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when we ingested it
    raw_payload     JSONB NOT NULL        -- verbatim event, for audit/replay
);

CREATE TABLE transactions (               -- current-state projection, read path
    transaction_id    UUID PRIMARY KEY,
    merchant_id       TEXT NOT NULL REFERENCES merchants(merchant_id),
    currency          CHAR(3) NOT NULL,

    initiated_at TIMESTAMPTZ, processed_at TIMESTAMPTZ,
    failed_at    TIMESTAMPTZ, settled_at   TIMESTAMPTZ, last_settled_at TIMESTAMPTZ,
    first_event_at TIMESTAMPTZ NOT NULL,   last_event_at TIMESTAMPTZ NOT NULL,

    initiated_amount NUMERIC(14,2), processed_amount NUMERIC(14,2),
    settlement_amount NUMERIC(14,2),

    event_count INTEGER NOT NULL DEFAULT 0,           -- per-type counters, see §5
    initiated_event_count INTEGER NOT NULL DEFAULT 0,
    processed_event_count INTEGER NOT NULL DEFAULT 0,
    failed_event_count    INTEGER NOT NULL DEFAULT 0,
    settled_event_count   INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Generated columns — the app never writes these; they can't drift.
    payment_amount NUMERIC(14,2) GENERATED ALWAYS AS
        (COALESCE(processed_amount, initiated_amount)) STORED,
    payment_status TEXT GENERATED ALWAYS AS (
        CASE WHEN failed_at    IS NOT NULL THEN 'failed'
             WHEN processed_at IS NOT NULL THEN 'processed'
             WHEN initiated_at IS NOT NULL THEN 'initiated'
             ELSE 'unknown' END) STORED,
    settlement_status TEXT GENERATED ALWAYS AS (
        CASE WHEN settled_at IS NOT NULL THEN 'settled' ELSE 'pending' END) STORED,
    status TEXT GENERATED ALWAYS AS (
        CASE WHEN failed_at    IS NOT NULL THEN 'failed'
             WHEN settled_at   IS NOT NULL THEN 'settled'
             WHEN processed_at IS NOT NULL THEN 'processed'
             WHEN initiated_at IS NOT NULL THEN 'initiated'
             ELSE 'unknown' END) STORED,

    CONSTRAINT chk_counts CHECK (event_count > 0)
);
```

Full DDL, including every index, is in `migrations/versions/0001_initial.py`.

**Indexes:**

```sql
-- listing/search
CREATE INDEX idx_txn_merchant_status_time ON transactions (merchant_id, status, initiated_at DESC);
CREATE INDEX idx_txn_status_time          ON transactions (status, initiated_at DESC);
CREATE INDEX idx_txn_initiated_at         ON transactions (initiated_at DESC);
CREATE INDEX idx_txn_last_event_at        ON transactions (last_event_at DESC);

-- one partial index per discrepancy class -- see §6 and §10
CREATE INDEX idx_txn_unsettled           ON transactions (processed_at) WHERE ...;
CREATE INDEX idx_txn_settled_after_fail  ON transactions (merchant_id) WHERE ...;
CREATE INDEX idx_txn_double_settled      ON transactions (merchant_id) WHERE settled_event_count > 1;
CREATE INDEX idx_txn_amount_mismatch     ON transactions (merchant_id) WHERE ...;
CREATE INDEX idx_txn_stuck               ON transactions (initiated_at) WHERE ...;
CREATE INDEX idx_txn_orphan_settlement   ON transactions (merchant_id) WHERE ...;
```

**Why no separate `settlements` table.** In this event stream a settlement
is just another event type on the same transaction, already durably
recorded in `payment_events`. A separate table would duplicate that log
with no new facts, and every reconciliation query would need an extra join.
The trigger to split it out: settlement *batches* (a file covering many
transactions, with its own UTR and value date) or genuinely partial
settlements — neither of which this event schema expresses.

**`event_timestamp` vs `received_at` are kept separate.** Event time orders
the lifecycle; ingestion time answers "how long has this been pending *to
us*". Collapsing them would make delivery lag indistinguishable from an
out-of-order event.

**No FK from `payment_events.transaction_id` to `transactions`.** The log
must accept an event even when it's the first thing we've ever heard about
that transaction, and must not depend on the projection existing.

---

## 4. API documentation

Full interactive reference at `/docs` (Swagger) and `/openapi.json`. Summary:

### `POST /events`

Accepts a single event object or `{"events": [...]}` (max 1,000 per
request). **Partial success by design**: one malformed event in a batch of
500 does not reject the other 499.

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

Response:

```json
{
  "received": 1, "inserted": 1, "duplicates": 0, "rejected": 0,
  "transactions_touched": 1,
  "results": [{"event_id": "b768...", "status": "inserted"}]
}
```

### `GET /transactions`

Query params: `merchant_id` (repeatable), `status` (repeatable),
`payment_status`, `settlement_status`, `from`/`to` (+ `date_field`),
`min_amount`/`max_amount`, `has_discrepancy`, `sort_by`, `sort_order`,
`limit` (≤200), `offset`. All filtering, sorting, counting and pagination
happen in one SQL statement.

### `GET /transactions/{transaction_id}`

Returns the transaction, its merchant, its full event history, and its
reconciliation status — the *exact same* discrepancy rules as
`/reconciliation/discrepancies`, scoped to one transaction (see §6).
`404` with the standard error envelope if unknown; `422` if the path
segment isn't a valid UUID.

### `GET /reconciliation/summary`

Query params: `group_by` (CSV of `merchant`, `date`, `status`),
`granularity` (`day`/`week`/`month`), `merchant_id`, `from`/`to`,
`include_totals`, `limit`/`offset`. Returns per-group transaction counts,
payment/settlement totals, `amount_delta`, and a `settled`/`failed`/
`unsettled` breakdown, plus an optional grand-total row.

### `GET /reconciliation/discrepancies`

Query params: `type` (repeatable, one of the 6 classes in §6),
`merchant_id`, `severity`, `from`/`to`, `unsettled_after_hours` (default
48), `stuck_after_hours` (default 24), `sort_by`, `limit`/`offset`.
Returns the flagged transactions plus `counts_by_type` (unaffected by the
`type` filter, so the page and the totals never disagree).

### `GET /health`

`{"status": "ok", "database": "ok", "version": "1.0.0", "event_count": N}`,
`503` if the DB is unreachable.

### Error envelope (every non-2xx response)

```json
{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": [...], "request_id": "..."}}
```

`400 BAD_REQUEST` · `404 TRANSACTION_NOT_FOUND` · `413 BATCH_TOO_LARGE` ·
`422 VALIDATION_ERROR` · `500 INTERNAL_ERROR` · `503 DATABASE_UNAVAILABLE`.
Every response carries an `X-Request-ID` header, echoed in the structured
JSON logs.

---

## 5. Idempotency

Three layers (`app/services/ingestion.py`):

1. **Storage-level dedup.** `event_id` is the `payment_events` primary key;
   ingestion is `INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING ...`.
   No "check then insert" in application code, so two concurrent replays
   can't both pass — the unique index serialises them. Catches the 190
   exact replays in the raw sample file.
2. **Projection updated only from rows that actually inserted.** The
   upsert into `transactions` runs in a CTE fed by step 1's `RETURNING`
   clause, in the *same* database transaction. A duplicate event produces
   no `inserted` row, so it can't touch the projection — the per-type
   counters (`settled_event_count`, etc.) cannot double-count. Every
   assignment in the `ON CONFLICT ... DO UPDATE` is `LEAST`/`GREATEST`/`+`
   over the existing row, evaluated by Postgres under the row lock, so
   there's no lost-update race and no need for `SELECT ... FOR UPDATE`.
3. **Detection for logical duplicates that carry a new `event_id`.** The
   sample data's 95 double-settlements are the same settlement
   re-announced with a *different* `event_id` — nothing at the write layer
   can safely suppress that (two genuine partial settlements would look
   identical; silently dropping one would be data loss in a money system).
   The projection counts them (`settled_event_count`) and
   `DOUBLE_SETTLEMENT` reports them for a human to resolve.

**Verified, not just asserted:** `python scripts/load_sample_data.py` run
twice against the same database reports `inserted: 10480, duplicates: 190`
on the first run and `inserted: 0, duplicates: 10480` on the second. Also
covered by `tests/test_ingestion.py` (7 tests, including a simulated
20-way concurrent replay and out-of-order arrival).

---

## 6. Discrepancy classes

All six live in `app/services/reconciliation.py` as a `UNION ALL` of small,
focused `SELECT`s — one per class, each hitting its own partial index —
rather than one large boolean expression that no index could serve.

| Code | Severity | Rule | Count in shipped data |
|---|---|---|---|
| `PROCESSED_NOT_SETTLED` | high | processed, no settlement after 48h (configurable) | 380 |
| `SETTLED_AFTER_FAILURE` | critical | settlement recorded against a failed payment | 95 |
| `DOUBLE_SETTLEMENT` | critical | more than one settlement event | 95 |
| `AMOUNT_MISMATCH` | critical | settled amount ≠ payment amount | 80 |
| `STUCK_INITIATED` | medium | initiated, nothing since, after 24h (configurable) | 190 |
| `ORPHAN_SETTLEMENT` | high | settlement with no matching initiation | 25 |

865 total discrepancies across the shipped 3,825-transaction dataset.
`GET /reconciliation/discrepancies` and `GET /transactions/{id}` are backed
by the *same* SQL (the detail endpoint scopes the same query to one
transaction_id) — there is one source of truth for what counts as a break,
not two implementations that can drift apart.

**The grace windows are the point.** A transaction processed 10 minutes ago
with no settlement yet is not a break, it's in flight — flagging it would
drown operations in false positives, which is the most common way a
reconciliation report gets ignored. `unsettled_after_hours` (default 48,
a T+2 assumption) and `stuck_after_hours` (default 24) are both query
parameters and settings, tested at the exact boundary
(`test_processed_not_settled_grace_window_boundary`: 47h → not flagged,
49h → flagged).

---

## 7. Sample data

### What the raw file actually contains

Profiled `sample_events.json` before designing anything: **10,355 events**,
of which **10,165 distinct `event_id`s** (190 exact byte-identical
replays), across **3,800 transactions** and **5 merchants**, Jan–Apr 2026,
INR only, amounts ₹135.92–₹49,964.11.

Transaction shapes: 2,565 clean (initiated→processed→settled), 570 clean
failures, 380 processed-never-settled, 190 stuck-at-initiated, 95
settled-after-failure, and 95 with two distinct `settled` events under
different `event_id`s minutes apart (a double settlement no idempotency
key can catch — see §5).

Two break classes the raw file does **not** exercise at all: amount
mismatches and out-of-order deliveries.

### Augmentation (`scripts/augment_sample_data.py`, seed=42, deterministic)

| Injected | Adds new events? | Count |
|---|---|---|
| Amount mismatch | no — modifies existing `settled` events | 80 |
| Out-of-order arrival | no — reorders existing events, timestamps unchanged | 50 |
| Orphan settlement | yes | +25 |
| Near-duplicate replay (fresh `event_id`, +2s) | yes | +100 |

Result: **10,480 events across 3,825 transactions**, all six discrepancy
classes populated. Both `sample_events.json` (raw) and
`sample_events_augmented.json` (used by the loader) ship in this repo.

### Loading

`scripts/load_sample_data.py` posts the file through `POST /events` in
batches of 500 — the same code path a real producer would use, so the
load itself exercises and times real ingestion. Measured: **10,480 events
in 1.3 seconds** end to end (well under the 30s target).

---

## 8. Deployment

**Public URL:** `<PASTE_RENDER_URL_HERE>`

Primary: **Render**, via `render.yaml` (Docker web service + managed
Postgres, `DATABASE_URL` injected automatically, health check at
`/health`, `alembic upgrade head` runs on container start). Chosen because
the whole topology — app, database, health check — lives in one file in
the repo, and no reviewer needs cloud credentials to reproduce it.

> **Cold starts on Render's free tier can take ~50 seconds** on the first
> request after a period of idleness. Hit `/health` first — an
> unexplained timeout on the very first request is the free tier waking
> up, not a broken deployment.

Fallback: **Fly.io** (`fly.toml`, not included by default — add if Render's
free Postgres is unavailable at submission time; the app is already
Dockerized so the image is identical).

**Local:** `docker compose up --build` (§2, Option A) or the no-Docker path
(§2, Option B).

---

## 9. Testing

**48 pytest tests, all against a real Postgres database** (`setu_test`) —
not SQLite, because the schema leans on Postgres-specific features
(generated columns, `ON CONFLICT ... DO UPDATE` with `EXCLUDED`, `FILTER`,
partial indexes, native enums) that SQLite can't express.

```bash
export TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/setu_test
createdb setu_test
DATABASE_URL=$TEST_DATABASE_URL alembic upgrade head
pytest tests/ -v
```

| File | Covers |
|---|---|
| `test_ingestion.py` (7) | exact replay, intra-batch duplicates, simulated 20-way concurrent replay, out-of-order arrival, **projection-rebuild equivalence** |
| `test_discrepancies.py` (15) | one fixture per discrepancy class, the 47h/49h grace-window boundary, detail-endpoint/discrepancies-endpoint agreement, elapsed-hours reason-string regression |
| `test_validation.py` (14) | negative/zero amount, bad enum, naive & future timestamps, malformed UUID, bad currency, oversized batch, 404/422 distinction, SQL-injection-shaped `sort_by` |
| `test_transactions.py` (9) | merchant/status/date filters, no-gap/no-overlap pagination, sort ordering, `has_discrepancy`, summary group-by and totals-row consistency |
| `test_config.py` (4) | `DATABASE_URL` scheme/driver normalization — `postgres://` and bare `postgresql://` both resolve to the installed psycopg2 driver |

Also included:

- **Postman collection** (`postman/`) — 7 folders covering every endpoint,
  plus a scripted idempotency proof (post → repost → fetch → assert
  `event_count` unchanged) with Postman-native test assertions on every
  request. The collection's `base_url` variable defaults to
  `http://localhost:8000` out of the box; **once deployed, update that one
  default value to the production URL** (or import
  `postman/production.postman_environment.json` with the URL filled in)
  so a reviewer can open the collection and run every request immediately,
  with no environment setup required.
- **Idempotency proof via the loader**, described in §5 and §7.

---

## 10. Performance

Rules followed throughout: no aggregation, filtering, sorting or
pagination in Python; one statement per endpoint (two for the detail view
— transaction row, then its events — deliberately not one join, which
would multiply the transaction row across every event); parameter binding
everywhere, with sort/group columns resolved through a whitelist dict so
user strings never reach SQL as raw identifiers; `Decimal` end to end,
never `float`, for money.

**Measured at current volume (3,825 transactions):** the planner
seq/index-scans directly — `EXPLAIN ANALYZE` on the `AMOUNT_MISMATCH` and
`DOUBLE_SETTLEMENT` partial-index queries both execute in **<0.2ms**. For
the transactions list query filtered by merchant + status, the planner
actually preferred the single-column `idx_txn_initiated_at` over the
composite `idx_txn_merchant_status_time` at this size — the table is small
enough that the two plans cost about the same, and it's worth stating
honestly rather than asserting the composite index is what's serving the
query.

**Measured at 419,200 events / 153,000 transactions** (40x the sample
file, generated with `augment_sample_data.py --scale 40`): the planner
**switches to the composite `idx_txn_merchant_status_time` index** for the
same list query, and the `AMOUNT_MISMATCH` / `DOUBLE_SETTLEMENT` partial
indexes still resolve in **6–8ms** scanning ~3,200–6,500 matching rows out
of 153,000. This is the crossover behaviour the index design was betting
on, observed rather than assumed.

**Ingestion throughput:** 10,480 events loaded end-to-end (batches of 500,
over real HTTP) in 1.3s; the 419,200-event scale test loaded in 48s over
420 HTTP round trips.

**Known tradeoff — offset pagination.** `OFFSET` is O(offset) and
degrades on deep pages. Chosen anyway because the brief's listing use case
(an operations console with jump-to-page) benefits from offset semantics
and the dataset here is small; keyset pagination
(`WHERE (initiated_at, transaction_id) < (:last_ts, :last_id)`) is the
documented next step if deep pagination at large scale becomes real.

**Scaling notes beyond what's built:** past roughly 50M events, range-
partition `payment_events` by month; consider a materialized view for
`/reconciliation/summary` if its aggregate latency crosses ~200ms; move
`total` counts to `pg_class.reltuples` estimates if `count(*) OVER ()`
becomes the bottleneck. None of these are needed yet — stated here so the
trigger condition is explicit rather than the omission being silent.

---

## 11. Assumptions and tradeoffs

**Assumptions:**
- Settlement SLA is T+2 → 48h default grace window for `PROCESSED_NOT_SETTLED`.
- `event_id` is producer-generated and globally unique.
- A transaction belongs to exactly one merchant and one currency (verified
  true across all 3,800 transactions in the raw sample).
- `payment_failed` is terminal on the payment side.
- Amounts are gross — no fees or FX modelled; currency is stored per-row
  but the service never converts.
- A settled amount differing from the payment amount, even by paise, is a
  real discrepancy worth surfacing (`AMOUNT_MISMATCH` has no tolerance
  band) — deliberately strict, on the theory that a reconciliation tool
  that hides small mismatches is worse than one that's noisy about them.

**Tradeoffs:**
- **Projection vs. pure event sourcing** — see §1. Licensed by the
  rebuild-equivalence test, not just asserted.
- **Offset vs. keyset pagination** — see §10.
- **`UNION ALL` of 6 small queries vs. one big boolean expression** for
  discrepancies — the six-query form lets each class use its own partial
  index; a single `array_remove(ARRAY[...], NULL)`-style query would be
  one scan instead of six but could use no index for any class.
- **`settlement_amount` reflects the latest settlement batch, not the
  first.** `COALESCE(EXCLUDED.settlement_amount, t.settlement_amount)`
  overwrites the column when a new batch contains a settlement — correct
  for `DOUBLE_SETTLEMENT` detection (we want the most recent figure) but
  means an earlier settlement amount isn't retained anywhere outside
  `payment_events` once a later one arrives. Acceptable because the event
  log stays authoritative and `settled_event_count > 1` is what actually
  flags the case for a human.
- **No auth.** `API_KEY` exists in settings/`.env.example` for forward
  compatibility but isn't wired into the routers — see §12.
- **Partial success on batch ingest** — one bad event doesn't fail the
  other 499; see §4.

---

## 12. What I'd do differently with more time

- Wire up the `API_KEY` setting into actual request-level enforcement.
- Keyset pagination alongside offset (§10), switchable via a `cursor` param.
- An `Idempotency-Key` header + `request_log` table for whole-batch retry
  safety when a client isn't sure if a POST landed (the table exists in
  the migration; nothing writes to it yet — event-level idempotency
  already makes replays safe, so this is a nice-to-have, not a gap).
- `OUT_OF_SEQUENCE` as a seventh discrepancy class (settlement timestamped
  before processing) — the augmented data exercises the *scenario* via the
  out-of-order injections, and correctly does *not* flag them (proving
  ordering depends on `event_timestamp`, not arrival order), but there's no
  dedicated class for a payment that's genuinely, permanently out of
  sequence rather than just delivered late.
- `week`/`month` granularity smoke tests for `/reconciliation/summary`
  (`day` is tested; the other two share the same `date_trunc` code path
  but aren't independently asserted).

