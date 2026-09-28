"""Initial schema: merchants, payment_events (log), transactions (projection).

Revision ID: 0001
Revises: None
Create Date: 2026-09-28
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE merchants (
            merchant_id   TEXT PRIMARY KEY,
            merchant_name TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TYPE event_type_enum AS ENUM
            ('payment_initiated', 'payment_processed', 'payment_failed', 'settled');
        """
    )

    op.execute(
        """
        CREATE TABLE payment_events (
            event_id        UUID PRIMARY KEY,
            transaction_id  UUID NOT NULL,
            merchant_id     TEXT NOT NULL REFERENCES merchants(merchant_id),
            event_type      event_type_enum NOT NULL,
            amount          NUMERIC(14,2) NOT NULL CHECK (amount > 0),
            currency        CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            event_timestamp TIMESTAMPTZ NOT NULL,
            received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            raw_payload     JSONB NOT NULL
        );
        """
    )
    op.execute("CREATE INDEX idx_events_txn  ON payment_events (transaction_id, event_timestamp);")
    op.execute("CREATE INDEX idx_events_merc ON payment_events (merchant_id, event_timestamp DESC);")
    op.execute("CREATE INDEX idx_events_type ON payment_events (event_type, event_timestamp DESC);")

    op.execute(
        """
        CREATE TABLE transactions (
            transaction_id    UUID PRIMARY KEY,
            merchant_id       TEXT NOT NULL REFERENCES merchants(merchant_id),
            currency          CHAR(3) NOT NULL,

            initiated_at      TIMESTAMPTZ,
            processed_at      TIMESTAMPTZ,
            failed_at         TIMESTAMPTZ,
            settled_at        TIMESTAMPTZ,
            last_settled_at   TIMESTAMPTZ,
            first_event_at    TIMESTAMPTZ NOT NULL,
            last_event_at     TIMESTAMPTZ NOT NULL,

            initiated_amount  NUMERIC(14,2),
            processed_amount  NUMERIC(14,2),
            settlement_amount NUMERIC(14,2),

            event_count            INTEGER NOT NULL DEFAULT 0,
            initiated_event_count  INTEGER NOT NULL DEFAULT 0,
            processed_event_count  INTEGER NOT NULL DEFAULT 0,
            failed_event_count     INTEGER NOT NULL DEFAULT 0,
            settled_event_count    INTEGER NOT NULL DEFAULT 0,

            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

            payment_amount NUMERIC(14,2)
                GENERATED ALWAYS AS (COALESCE(processed_amount, initiated_amount)) STORED,

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
        """
    )

    op.execute(
        "CREATE INDEX idx_txn_merchant_status_time ON transactions (merchant_id, status, initiated_at DESC);"
    )
    op.execute("CREATE INDEX idx_txn_status_time ON transactions (status, initiated_at DESC);")
    op.execute("CREATE INDEX idx_txn_initiated_at ON transactions (initiated_at DESC);")
    op.execute("CREATE INDEX idx_txn_last_event_at ON transactions (last_event_at DESC);")

    op.execute(
        """
        CREATE INDEX idx_txn_unsettled ON transactions (processed_at)
            WHERE settled_at IS NULL AND failed_at IS NULL AND processed_at IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_txn_settled_after_fail ON transactions (merchant_id)
            WHERE failed_at IS NOT NULL AND settled_at IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_txn_double_settled ON transactions (merchant_id)
            WHERE settled_event_count > 1;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_txn_amount_mismatch ON transactions (merchant_id)
            WHERE settlement_amount IS NOT NULL
              AND settlement_amount <> COALESCE(processed_amount, initiated_amount);
        """
    )
    op.execute(
        """
        CREATE INDEX idx_txn_stuck ON transactions (initiated_at)
            WHERE processed_at IS NULL AND failed_at IS NULL AND settled_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_txn_orphan_settlement ON transactions (merchant_id)
            WHERE settled_at IS NOT NULL AND initiated_at IS NULL;
        """
    )

    # Optional idempotency-key table (§5.4 in the spec) — cheap to include now.
    op.execute(
        """
        CREATE TABLE request_log (
            idempotency_key TEXT PRIMARY KEY,
            response_body   JSONB NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS request_log;")
    op.execute("DROP TABLE IF EXISTS transactions;")
    op.execute("DROP TABLE IF EXISTS payment_events;")
    op.execute("DROP TYPE IF EXISTS event_type_enum;")
    op.execute("DROP TABLE IF EXISTS merchants;")
