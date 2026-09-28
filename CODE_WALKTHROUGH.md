# Code Walkthrough — How To Explain This Project

This is a narration script, not a spec. Read it in order once before the
interview; in the interview, follow the same order — **container boot →
one write path → one read path → the reconciliation logic** — and you
will have covered every piece a reviewer cares about without needing to
jump around the codebase.

---

## 0. The one-sentence version

*"A payment lifecycle event log gets written once and never mutated;
everything else — transaction status, summaries, discrepancies — is
derived from that log by SQL, not by application code."*

Say this first. Everything below is just unpacking that sentence.

---

## 1. Where execution actually starts

Not `app/main.py` — **the container's `CMD`**. This is the first thing
to point at, because it answers "how does this even boot":

```dockerfile
# Dockerfile
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

Two things happen, in order, every time the container starts:

1. **`alembic upgrade head`** — applies the schema migration. It's
   idempotent: if the schema is already up to date, this is a no-op, so
   restarting the container never re-runs anything destructive.
2. **`uvicorn app.main:app`** — starts the ASGI server, which imports
   `app/main.py` and runs the object named `app` inside it.

**Interview line:** *"I always start people at the Dockerfile, not
`main.py`, because that's the actual first instruction that runs — it
tells you the migration is part of the boot sequence, not a manual step
someone has to remember."*

---

## 2. `app/main.py` — the second thing that runs

```python
# app/main.py
from app.api import events, reconciliation, transactions
from app.core.config import settings
from app.core.db import engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging

configure_logging()

app = FastAPI(title="Setu Assignment — Partner Reconciliation Service", ...)

register_exception_handlers(app)          # every non-2xx response, uniform shape
app.include_router(events.router)          # POST /events
app.include_router(transactions.router)    # GET /transactions, GET /transactions/{id}
app.include_router(reconciliation.router)  # GET /reconciliation/summary, /discrepancies

@app.get("/health")
def health():
    ...
```

This file does four things, in this order, and that order matters:

1. Configure logging *before* anything else can log
2. Build the `FastAPI` app object
3. Register the error-handling middleware *before* registering routes
   (so every route, even ones added later, inherits the same error shape)
4. Mount three routers — one per logical area of the API

**Interview line:** *"This file is deliberately thin — it's wiring, not
logic. If you're looking for where a request is actually handled, you go
into `app/api/`, not here."*

---

## 3. The four supporting modules under `app/core/`

Point at these briefly — they're infrastructure, not business logic, and
naming them shows you separated concerns on purpose.

| File | One line |
|---|---|
| `config.py` | `Settings` — one Pydantic object holding every tunable (grace windows, page size caps, `DATABASE_URL`). Loaded once from environment variables. |
| `db.py` | The SQLAlchemy `engine`, `SessionLocal`, and the `get_db()` FastAPI dependency every route uses to get a session. |
| `errors.py` | `AppError` + exception handlers — every error response has the same JSON shape: `{"error": {"code", "message", "details", "request_id"}}`. |
| `logging.py` | Structured (JSON) log formatting. |

**The one thing worth actually opening code for here** — `config.py` has
a validator that's a genuinely interesting story (see §8, "the deploy
bugs" — save it for when they ask about production concerns).

---

## 4. `app/models.py` and the migration — what the database actually looks like

Two files describe the schema; they must agree, and here's why there are
two:

- **`migrations/versions/0001_initial.py`** — the DDL that actually
  creates the tables. Written as raw SQL (`op.execute(...)`), not
  SQLAlchemy's table-builder, because the schema needs generated columns,
  `CHECK` constraints, and partial indexes that the table-builder can't
  express.
- **`app/models.py`** — SQLAlchemy ORM classes that *mirror* that DDL, so
  the rest of the app can use the ORM for reads. They're read-only mirrors
  — nothing writes through them (see §6).

**The three tables, in the order to introduce them:**

```
merchants        -- dimension table: id + display name
payment_events   -- append-only log. NEVER updated. system of record.
transactions     -- derived projection. one row per transaction. read path.
```

**Interview line:** *"`payment_events` is the source of truth.
`transactions` is a cache that's always computable from the log — I have
a script (`scripts/rebuild_projection.py`) that proves this by rebuilding
`transactions` from scratch and asserting it's identical to what
incremental updates produced. That test is what lets me treat the
denormalisation as safe rather than as a shortcut."*

Show the four generated columns on `transactions` if they ask "how do
you compute status":

```sql
status TEXT GENERATED ALWAYS AS (
    CASE WHEN failed_at    IS NOT NULL THEN 'failed'
         WHEN settled_at   IS NOT NULL THEN 'settled'
         WHEN processed_at IS NOT NULL THEN 'processed'
         WHEN initiated_at IS NOT NULL THEN 'initiated'
         ELSE 'unknown' END) STORED,
```

**Why this matters:** the app never writes `status` — Postgres computes
it from the four timestamp columns every time a row changes. It cannot
drift out of sync with the facts, because it's not a separate fact.

---

## 5. Walk the write path: `POST /events`

This is the flow to narrate in the most detail, because idempotency is
the assignment's most heavily-weighted requirement.

### 5a. The request arrives — `app/api/events.py`

```python
@router.post("/events")
def post_events(payload: Union[PaymentEventIn, EventsBatchIn] = Body(...), db: Session = Depends(get_db)):
    ...
    valid_events = [ev for ev in raw_events if passes validation]
    rejected_results = [... for anything that failed validation ...]
    ingest_result = ingest_events(db, valid_events)
    return {...counts..., "results": ingest_result["results"] + rejected_results}
```

Two things to call out:
- **Pydantic validates before this function body even runs** — `PaymentEventIn`
  (in `app/schemas.py`) rejects negative amounts, bad UUIDs, naive
  timestamps, future timestamps, and non-3-letter currency codes at the
  framework level.
- **Partial success is deliberate.** One bad event in a batch of 500
  doesn't fail the other 499 — each event is validated independently, and
  only the valid ones go to `ingest_events`.

### 5b. The actual idempotency mechanism — `app/services/ingestion.py`

This is the heart of the project. Walk it as three layers:

**Layer 1 — storage-level dedup, one atomic statement:**

```sql
INSERT INTO payment_events (event_id, transaction_id, ..., raw_payload)
SELECT * FROM unnest(:event_ids ::uuid[], :txn_ids ::uuid[], ...)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id, transaction_id, merchant_id, event_type, amount, event_timestamp;
```

`event_id` is the **primary key** on `payment_events` — it's producer-supplied,
so it *is* the natural idempotency key. There's no "check if it exists,
then insert" in application code — that pattern has a race window between
the check and the insert. This is one atomic statement; the database's
unique index is what actually prevents the duplicate, so two concurrent
replays of the same event can't both succeed.

**Layer 2 — the projection is updated only from rows that actually inserted:**

```sql
WITH rolled AS (
    SELECT transaction_id, ...,
           min(event_timestamp) FILTER (WHERE event_type = 'payment_initiated') AS initiated_at,
           ...
    FROM unnest(...)   -- fed by Layer 1's RETURNING clause
    GROUP BY transaction_id, merchant_id, currency
)
INSERT INTO transactions (...)
SELECT * FROM rolled
ON CONFLICT (transaction_id) DO UPDATE SET
    initiated_at = LEAST(t.initiated_at, EXCLUDED.initiated_at),
    settled_at   = LEAST(t.settled_at,   EXCLUDED.settled_at),
    event_count  = t.event_count + EXCLUDED.event_count,
    ...
```

Three properties to explain here, because this is where interviewers dig:

1. **A duplicate event produces no row in Layer 1's `RETURNING`, so it
   never reaches this statement at all** — the counters literally cannot
   double-count a replay, because a replay never gets this far.
2. **No lost-update race under concurrency.** Every assignment is an
   expression over the *existing* row (`LEAST`, `GREATEST`, `+`),
   evaluated by Postgres while it holds the row lock. There's no
   `SELECT` then separate `UPDATE` in Python, so there's no window for
   two concurrent requests to clobber each other.
3. **Order-independence.** `LEAST(t.initiated_at, EXCLUDED.initiated_at)`
   means a late-arriving `payment_initiated` correctly backfills
   `initiated_at` no matter what order the events physically arrived in.
   Ordering comes from `event_timestamp`, never from arrival sequence.

**Layer 3 — the honest limit.** Some duplicates arrive with a *different*
`event_id` — the sample data has 95 transactions with two distinct
`settled` events, minutes apart, same amount. No idempotency key can
catch that safely (a genuine partial settlement would look identical;
silently dropping one would be data loss in a money system). Instead,
`settled_event_count` tracks it, and it's surfaced as a `DOUBLE_SETTLEMENT`
discrepancy for a human to resolve. **This is the single best thing to
say if asked "is your idempotency bulletproof" — the honest answer is
"at the event level, yes, provably; at the logical level, no system can
be, and here's how I surface that instead of pretending otherwise."**

Both SQL statements run inside one database transaction — there is never
a moment where an event exists in the log but not in the projection.

---

## 6. Walk a read path: `GET /transactions/{transaction_id}`

Shorter to narrate, but shows a different skill: **not duplicating logic**.

```python
# app/api/transactions.py
@router.get("/transactions/{transaction_id}")
def get_transaction(transaction_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.execute(txn_sql, {...}).mappings().first()
    if row is None:
        raise HTTPException(404, {"code": "TRANSACTION_NOT_FOUND", ...})

    event_rows = db.execute(events_sql, {"tid": ...}).mappings().all()

    break_rows, _, _ = query_discrepancies(db, ..., transaction_id=str(transaction_id))
    discrepancies = [{"type": b["discrepancy_type"], ...} for b in break_rows]

    return {"transaction": ..., "merchant": ..., "reconciliation": {...}, "events": [...]}
```

**The interesting detail:** `query_discrepancies` is the *exact same
function* `GET /reconciliation/discrepancies` calls — just scoped to one
`transaction_id`. Earlier in building this, the detail endpoint had its
own hand-written Python re-implementation of the six discrepancy rules,
and it silently missed `DOUBLE_SETTLEMENT` because it forgot to fetch one
column. Fixed by deleting that duplication entirely, so there's one
source of truth for "what counts as a break" instead of two
implementations that can drift apart.

**Interview line:** *"That bug is actually a good thing to have found and
fixed, not something to hide — it's evidence I tested against real data
and caught a real correctness issue, and the fix is a better design than
what I had before, not just a patch."*

---

## 7. The reconciliation SQL — `app/services/reconciliation.py`

This is where "aggregations and filtering happen in SQL, not Python loops"
gets proven, not just claimed.

**Six discrepancy classes, each a small `SELECT`, combined with `UNION ALL`:**

```python
_DISCREPANCY_DEFS = [
    ("PROCESSED_NOT_SETTLED", "high", "<reason expr>",
     "t.processed_at IS NOT NULL AND t.settled_at IS NULL AND t.failed_at IS NULL "
     "AND t.processed_at < now() - (:unsettled_after_hours || ' hours')::interval"),
    ("SETTLED_AFTER_FAILURE", "critical", ..., "t.failed_at IS NOT NULL AND t.settled_at IS NOT NULL"),
    ("DOUBLE_SETTLEMENT",     "critical", ..., "t.settled_event_count > 1"),
    ("AMOUNT_MISMATCH",       "critical", ..., "t.settlement_amount <> COALESCE(t.processed_amount, t.initiated_amount)"),
    ("STUCK_INITIATED",       "medium",   ..., "... AND t.initiated_at < now() - (:stuck_after_hours || ' hours')::interval"),
    ("ORPHAN_SETTLEMENT",     "high",     ..., "t.settled_at IS NOT NULL AND t.initiated_at IS NULL"),
]
```

**Why six small queries instead of one big `WHERE` with six `OR`
branches:** each class has its own partial index —

```sql
CREATE INDEX idx_txn_amount_mismatch ON transactions (merchant_id)
    WHERE settlement_amount IS NOT NULL
      AND settlement_amount <> COALESCE(processed_amount, initiated_amount);
```

— and Postgres can only use a partial index if the query's `WHERE` clause
matches it closely. A single query with six `OR`ed conditions couldn't
use any of the six partial indexes; six separate queries, each shaped to
match its own index, each do. Measured this directly: at 153,000
transactions (a 40x-scaled synthetic run), the `AMOUNT_MISMATCH` and
`DOUBLE_SETTLEMENT` queries resolve in 6–8ms scanning only the few
thousand matching rows — not the full table.

**Grace windows are the point, not a detail.** A transaction processed 10
minutes ago with no settlement yet isn't broken, it's in flight — flagging
it would drown the discrepancies list in noise. `unsettled_after_hours`
(default 48, a T+2 assumption) and `stuck_after_hours` (default 24) are
both request parameters, and there's a test asserting the exact boundary:
47h since processing → not flagged; 49h → flagged.

---

## 8. If they ask "what went wrong / what did you catch" — the deploy bugs

This is a strong section to have ready verbatim, because it's evidence of
testing discipline, not just code that happens to work.

**Bug 1 — a Postgres cast breaks SQLAlchemy's parameter parser.**
`:amounts::numeric[]` (no space before `::`) silently fails to substitute
— the literal string reaches the database and errors there, not at the
ORM layer. Fix: a space before every cast (`:amounts ::numeric[]`).
Caught by actually running the first ingestion call against real Postgres.

**Bug 2 — `postgres://` isn't a valid SQLAlchemy dialect name.** Several
managed-Postgres providers (Render historically included) hand out
connection strings with that scheme; SQLAlchemy 1.4+ doesn't recognize it
and raises `NoSuchModuleError` at the very first `create_engine()` call —
meaning the container would crash-loop before serving a single request.

**Bug 3 — bare `postgresql://` (no driver suffix) defaults to `psycopg`
(v3) in SQLAlchemy 2.1**, not `psycopg2`, which is the only driver this
project actually installs. Same failure mode, different cause.

Both 2 and 3 fixed in one place — a Pydantic validator on
`Settings.DATABASE_URL`:

```python
@field_validator("DATABASE_URL")
@classmethod
def normalize_scheme(cls, v: str) -> str:
    if v.startswith("postgres://"):
        v = "postgresql://" + v[len("postgres://"):]
    if v.startswith("postgresql://"):
        v = "postgresql+psycopg2://" + v[len("postgresql://"):]
    return v
```

Confirmed by actually running the Dockerfile's exact startup command
(`alembic upgrade head && uvicorn ...`) against a `postgres://`-style URL
before ever deploying for real.

**Bug 4 — `extract(hour from interval)` returns only the 0–23 hour-of-day
component, not total elapsed time.** A discrepancy's human-readable
`reason` text said "Initiated 11h ago" for a transaction stuck since
January — actually ~6,300 hours. The detection logic itself was never
wrong (the `WHERE` predicates compare timestamps directly); only the
explanation text was. Caught by loading real sample data — spanning
Jan–Apr 2026 — into a database where "now" is September 2026, and
noticing the number looked wrong. Fixed with `extract(epoch from ...) / 3600`.

**Interview line:** *"None of these were found by re-reading the code —
all four were found by actually running it against real data and a real
clock. That's the argument for the testing discipline, not the code
itself: reading correct-looking code doesn't catch a bug in how a
function returns an interval component."*

---

## 9. If they ask "why not X" — the tradeoffs, ready to state directly

| Question | One-sentence answer |
|---|---|
| Why not just replay the event log on every read? | No index can serve a filtered query against a full-table aggregation — every request would degrade linearly with total event volume. |
| Why not drop the log and keep only current state? | Then `/transactions/{id}` has no event history, and there's nothing to rebuild the projection from if it's ever wrong. |
| Why not a `settlements` table? | In this event schema, a settlement is just another event type on the same transaction — a separate table would duplicate the log with no new facts. The real trigger to add one: settlement *batches* with their own UTR and value date (which is genuinely how Razorpay's real settlement API works) — this event schema doesn't carry that yet. |
| Why offset pagination, not keyset? | The brief's use case is an ops console with jump-to-page, and the dataset here is small enough that `OFFSET`'s O(offset) cost doesn't matter yet; keyset is the documented next step if deep pagination at scale becomes real. |
| Why six small discrepancy queries instead of one? | Partial-index matching — see §7. |
| Is `AMOUNT_MISMATCH` too strict (zero tolerance)? | Deliberately strict — a reconciliation tool that hides small mismatches is worse than one that's noisy about them. Configurable if it turns out to be too noisy in practice. |

---

## 10. Full source, for reference while narrating

The zip contains every file mentioned above. Suggested reading order if
you want to re-read before the interview, matching this document's order:

```
Dockerfile                          → §1
app/main.py                         → §2
app/core/config.py                  → §3, §8
app/core/db.py, errors.py           → §3
migrations/versions/0001_initial.py → §4
app/models.py                       → §4
app/schemas.py                      → §5a (validation rules)
app/api/events.py                   → §5a
app/services/ingestion.py           → §5b  ← spend the most time here
app/api/transactions.py             → §6
app/services/reconciliation.py      → §7   ← second most time here
app/api/reconciliation.py           → §7 (the HTTP-layer params)
tests/                               → evidence for every claim above
```
