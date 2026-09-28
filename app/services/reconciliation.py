"""Reconciliation summary and discrepancy detection.

All six discrepancy classes are expressed as a UNION ALL of small, focused
SELECTs -- one per class -- so each can use its own partial index (see
migrations/versions/0001_initial.py) rather than one big boolean expression
that no index could serve. See TECH_SPEC.md §6.5 / §7 for the rationale.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

# Each entry: (code, severity, reason SQL expression, WHERE predicate)
# The predicate is written to line up with the matching partial index.
_DISCREPANCY_DEFS = [
    (
        "PROCESSED_NOT_SETTLED",
        "high",
        "'Payment processed ' || round(extract(epoch from (now() - t.processed_at)) / 3600)::int || 'h ago but never settled'",
        "t.processed_at IS NOT NULL AND t.settled_at IS NULL AND t.failed_at IS NULL "
        "AND t.processed_at < now() - (:unsettled_after_hours || ' hours')::interval",
    ),
    (
        "SETTLED_AFTER_FAILURE",
        "critical",
        "'Settlement recorded (' || t.settlement_amount || ') against a payment marked failed'",
        "t.failed_at IS NOT NULL AND t.settled_at IS NOT NULL",
    ),
    (
        "DOUBLE_SETTLEMENT",
        "critical",
        "t.settled_event_count || ' distinct settlement events recorded for this transaction'",
        "t.settled_event_count > 1",
    ),
    (
        "AMOUNT_MISMATCH",
        "critical",
        "'Settlement amount ' || t.settlement_amount || ' differs from payment amount ' "
        "|| COALESCE(t.processed_amount, t.initiated_amount) || ' (delta ' "
        "|| (t.settlement_amount - COALESCE(t.processed_amount, t.initiated_amount)) || ')'",
        "t.settlement_amount IS NOT NULL "
        "AND t.settlement_amount <> COALESCE(t.processed_amount, t.initiated_amount)",
    ),
    (
        "STUCK_INITIATED",
        "medium",
        "'Initiated ' || round(extract(epoch from (now() - t.initiated_at)) / 3600)::int || 'h ago with no further events'",
        "t.processed_at IS NULL AND t.failed_at IS NULL AND t.settled_at IS NULL "
        "AND t.initiated_at < now() - (:stuck_after_hours || ' hours')::interval",
    ),
    (
        "ORPHAN_SETTLEMENT",
        "high",
        "'Settlement recorded for a transaction never seen at initiation'",
        "t.settled_at IS NOT NULL AND t.initiated_at IS NULL",
    ),
]

# The base predicate every discrepancy class shares (merchant + date filters).
_COMMON_FILTERS = """
    AND (:merchant_ids IS NULL OR t.merchant_id = ANY(:merchant_ids ::text[]))
    AND (:from_ts IS NULL OR t.initiated_at >= :from_ts)
    AND (:to_ts IS NULL OR t.initiated_at < :to_ts)
"""


def _discrepancy_union_sql() -> str:
    """Build the UNION ALL of all six discrepancy classes."""
    parts = []
    for code, severity, reason_expr, predicate in _DISCREPANCY_DEFS:
        parts.append(
            f"""
            SELECT
                t.transaction_id, t.merchant_id, m.merchant_name,
                '{code}'::text AS discrepancy_type,
                '{severity}'::text AS severity,
                t.status,
                t.payment_amount, t.settlement_amount,
                (t.settlement_amount - t.payment_amount) AS amount_delta,
                extract(epoch FROM (now() - t.last_event_at)) / 3600.0 AS age_hours,
                ({reason_expr}) AS reason
            FROM transactions t
            JOIN merchants m USING (merchant_id)
            WHERE ({predicate})
            {_COMMON_FILTERS}
            AND (:types IS NULL OR '{code}' = ANY(:types ::text[]))
            AND (:severity IS NULL OR '{severity}' = :severity)
            """
        )
    return "\nUNION ALL\n".join(parts)


def query_discrepancies(
    db: Session,
    *,
    merchant_ids: list[str] | None,
    types: list[str] | None,
    severity: str | None,
    from_ts,
    to_ts,
    unsettled_after_hours: int,
    stuck_after_hours: int,
    sort_by: str,
    limit: int,
    offset: int,
    transaction_id: str | None = None,
) -> tuple[list[dict], dict[str, int], int]:
    union_sql = _discrepancy_union_sql()

    sort_map = {
        "detected_amount": "abs(coalesce(amount_delta, payment_amount, 0)) DESC",
        "age": "age_hours DESC",
        "merchant": "merchant_id ASC",
        "type": "discrepancy_type ASC",
    }
    order_clause = sort_map.get(sort_by, sort_map["detected_amount"])

    full_sql = text(
        f"""
        WITH breaks AS ( {union_sql} )
        SELECT *, count(*) OVER () AS total_count
        FROM breaks
        WHERE (:transaction_id IS NULL OR transaction_id = :transaction_id)
        ORDER BY {order_clause}, transaction_id
        LIMIT :limit OFFSET :offset;
        """
    )

    params = {
        "merchant_ids": merchant_ids,
        "types": types,
        "severity": severity,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "unsettled_after_hours": unsettled_after_hours,
        "stuck_after_hours": stuck_after_hours,
        "limit": limit,
        "offset": offset,
        "transaction_id": transaction_id,
    }
    rows = [dict(r) for r in db.execute(full_sql, params).mappings().all()]
    total = rows[0]["total_count"] if rows else 0

    counts_sql = text(
        f"""
        WITH breaks AS ( {union_sql} )
        SELECT discrepancy_type, count(*) AS n
        FROM breaks
        GROUP BY discrepancy_type;
        """
    )
    # counts_by_type ignores the type/severity filter that scopes the page,
    # but still respects merchant/date, so totals and the page stay
    # consistent when the user is browsing one type at a time.
    counts_params = dict(params)
    counts_params["types"] = None
    counts_params["severity"] = None
    count_rows = db.execute(counts_sql, counts_params).mappings().all()
    counts_by_type = {r["discrepancy_type"]: r["n"] for r in count_rows}

    return rows, counts_by_type, total


_GROUP_DIMENSIONS = {
    "merchant": ("t.merchant_id", "m.merchant_name"),
    "date": ("date_trunc(:granularity, t.initiated_at)", None),
    "status": ("t.status", None),
}


def query_summary(
    db: Session,
    *,
    group_by: list[str],
    granularity: str,
    merchant_ids: list[str] | None,
    from_ts,
    to_ts,
    include_totals: bool,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    select_cols = []
    group_cols = []
    if "merchant" in group_by:
        select_cols.append("t.merchant_id")
        select_cols.append("m.merchant_name")
        group_cols += ["t.merchant_id", "m.merchant_name"]
    if "date" in group_by:
        select_cols.append("date_trunc(:granularity, t.initiated_at) AS bucket_date")
        group_cols.append("date_trunc(:granularity, t.initiated_at)")
    if "status" in group_by:
        select_cols.append("t.status")
        group_cols.append("t.status")

    select_clause = (", ".join(select_cols) + ",") if select_cols else ""
    group_clause = ("GROUP BY " + ", ".join(group_cols)) if group_cols else ""

    base_sql = f"""
        SELECT
            {select_clause}
            count(*)                                                          AS transaction_count,
            coalesce(sum(t.payment_amount), 0)                                AS total_payment_amount,
            coalesce(sum(t.settlement_amount), 0)                             AS total_settlement_amount,
            coalesce(sum(t.settlement_amount - t.payment_amount)
                         FILTER (WHERE t.settlement_amount IS NOT NULL), 0)    AS amount_delta,
            count(*) FILTER (WHERE t.settlement_status = 'settled')           AS settled_count,
            count(*) FILTER (WHERE t.payment_status = 'failed')               AS failed_count,
            count(*) FILTER (WHERE t.payment_status = 'processed'
                               AND t.settlement_status = 'pending')           AS unsettled_count,
            sum(t.event_count)                                                AS event_count,
            count(*) OVER ()                                                  AS total_groups
        FROM transactions t
        JOIN merchants m USING (merchant_id)
        WHERE (:merchant_ids IS NULL OR t.merchant_id = ANY(:merchant_ids ::text[]))
          AND (:from_ts IS NULL OR t.initiated_at >= :from_ts)
          AND (:to_ts IS NULL OR t.initiated_at < :to_ts)
        {group_clause}
        ORDER BY {", ".join(group_cols) if group_cols else "1"}
        LIMIT :limit OFFSET :offset;
    """
    params = {
        "merchant_ids": merchant_ids,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "granularity": granularity,
        "limit": limit,
        "offset": offset,
    }
    rows = [dict(r) for r in db.execute(text(base_sql), params).mappings().all()]
    total = rows[0]["total_groups"] if rows else 0

    if include_totals:
        totals_sql = f"""
            SELECT
                count(*)                                                          AS transaction_count,
                coalesce(sum(t.payment_amount), 0)                                AS total_payment_amount,
                coalesce(sum(t.settlement_amount), 0)                             AS total_settlement_amount,
                coalesce(sum(t.settlement_amount - t.payment_amount)
                             FILTER (WHERE t.settlement_amount IS NOT NULL), 0)    AS amount_delta,
                count(*) FILTER (WHERE t.settlement_status = 'settled')           AS settled_count,
                count(*) FILTER (WHERE t.payment_status = 'failed')               AS failed_count,
                count(*) FILTER (WHERE t.payment_status = 'processed'
                                   AND t.settlement_status = 'pending')           AS unsettled_count,
                sum(t.event_count)                                                AS event_count
            FROM transactions t
            WHERE (:merchant_ids IS NULL OR t.merchant_id = ANY(:merchant_ids ::text[]))
              AND (:from_ts IS NULL OR t.initiated_at >= :from_ts)
              AND (:to_ts IS NULL OR t.initiated_at < :to_ts);
        """
        total_row = db.execute(text(totals_sql), params).mappings().first()
        if total_row:
            d = dict(total_row)
            d["is_total_row"] = True
            rows.append(d)

    return rows, total


# Reused by GET /transactions?has_discrepancy=true — a lightweight EXISTS-style
# boolean, built from the same six predicates, evaluated inline (no UNION,
# no need for it — this is a single boolean column, not a break listing).
HAS_DISCREPANCY_SQL = f"""(
    ({_DISCREPANCY_DEFS[0][3].replace(':unsettled_after_hours', ':unsettled_after_hours')})
    OR ({_DISCREPANCY_DEFS[1][3]})
    OR ({_DISCREPANCY_DEFS[2][3]})
    OR ({_DISCREPANCY_DEFS[3][3]})
    OR ({_DISCREPANCY_DEFS[4][3].replace(':stuck_after_hours', ':stuck_after_hours')})
    OR ({_DISCREPANCY_DEFS[5][3]})
)"""
