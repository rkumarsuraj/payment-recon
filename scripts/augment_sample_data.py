#!/usr/bin/env python3
"""Augment sample_events.json with break classes the raw file doesn't contain.

The raw file (profiled in TECH_SPEC.md §3.1) has zero amount mismatches and
zero out-of-order deliveries. This script adds:

  - 80 AMOUNT_MISMATCH cases    (modifies existing settled events in place)
  - 50 OUT_OF_SEQUENCE cases    (reorders existing events in the file; no new events)
  - 25 ORPHAN_SETTLEMENT cases  (+25 new events, +25 new transactions)
  - 100 near-duplicate replays  (+100 new events, same transactions)

Deterministic (fixed seed) so the numbers in the README are reproducible.

Usage:
    python scripts/augment_sample_data.py \
        --input sample_events.json \
        --output sample_events_augmented.json \
        [--scale N]   # repeat the whole augmented set N times with fresh
                       # ids/timestamps, for performance testing at volume
"""
import argparse
import copy
import json
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SEED = 42


def load(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def group_by_transaction(events: list[dict]) -> dict[str, list[dict]]:
    tx = defaultdict(list)
    for e in events:
        tx[e["transaction_id"]].append(e)
    return tx


def inject_amount_mismatches(events: list[dict], rng: random.Random, n: int) -> int:
    tx = group_by_transaction(events)
    clean_settled = [
        t
        for t, evs in tx.items()
        if {e["event_type"] for e in evs} == {"payment_initiated", "payment_processed", "settled"}
    ]
    rng.shuffle(clean_settled)
    changed = 0
    for t in clean_settled[:n]:
        settled_events = [e for e in events if e["transaction_id"] == t and e["event_type"] == "settled"]
        if not settled_events:
            continue
        ev = settled_events[0]
        original = ev["amount"]
        if rng.random() < 0.7:
            # short settlement: 0.5% - 5% short
            pct = rng.uniform(0.005, 0.05)
            ev["amount"] = round(original * (1 - pct), 2)
        else:
            # over-settlement
            pct = rng.uniform(0.005, 0.03)
            ev["amount"] = round(original * (1 + pct), 2)
        changed += 1
    return changed


def inject_out_of_order(events: list[dict], rng: random.Random, n: int) -> int:
    tx = group_by_transaction(events)
    candidates = [
        t
        for t, evs in tx.items()
        if {e["event_type"] for e in evs} == {"payment_initiated", "payment_processed", "settled"}
    ]
    rng.shuffle(candidates)
    changed = 0
    for t in candidates[:n]:
        evs = [e for e in events if e["transaction_id"] == t]
        processed = [e for e in evs if e["event_type"] == "payment_processed"]
        settled = [e for e in evs if e["event_type"] == "settled"]
        if not processed or not settled:
            continue
        # Swap the *file order* of the two events without changing their
        # timestamps -- this tests that ordering logic depends on
        # event_timestamp, not on arrival order, since our ingestion is
        # order-independent by construction (LEAST/GREATEST + FILTER).
        pi, si = events.index(processed[0]), events.index(settled[0])
        events[pi], events[si] = events[si], events[pi]
        changed += 1
    return changed


def inject_orphan_settlements(events: list[dict], rng: random.Random, n: int, merchants: list[dict]) -> list[dict]:
    new_events = []
    base_time = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for _ in range(n):
        m = rng.choice(merchants)
        ts = base_time + timedelta(minutes=rng.randint(0, 60 * 24 * 30))
        new_events.append(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "settled",
                "transaction_id": str(uuid.uuid4()),  # never seen before -> orphan
                "merchant_id": m["merchant_id"],
                "merchant_name": m["merchant_name"],
                "amount": round(rng.uniform(200, 40000), 2),
                "currency": "INR",
                "timestamp": ts.isoformat(),
            }
        )
    return new_events


def inject_near_duplicates(events: list[dict], rng: random.Random, n: int) -> list[dict]:
    non_settlement = [e for e in events if e["event_type"] != "settled"]
    rng.shuffle(non_settlement)
    new_events = []
    for e in non_settlement[:n]:
        dup = copy.deepcopy(e)
        dup["event_id"] = str(uuid.uuid4())
        ts = datetime.fromisoformat(e["timestamp"])
        dup["timestamp"] = (ts + timedelta(seconds=2)).isoformat()
        new_events.append(dup)
    return new_events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="sample_events.json")
    parser.add_argument("--output", default="sample_events_augmented.json")
    parser.add_argument("--scale", type=int, default=1, help="repeat the augmented set N times, for volume testing")
    args = parser.parse_args()

    rng = random.Random(SEED)
    events = load(args.input)

    merchants = []
    seen_m = set()
    for e in events:
        if e["merchant_id"] not in seen_m:
            merchants.append({"merchant_id": e["merchant_id"], "merchant_name": e["merchant_name"]})
            seen_m.add(e["merchant_id"])

    n_mismatch = inject_amount_mismatches(events, rng, 80)
    n_ooo = inject_out_of_order(events, rng, 50)
    orphan_events = inject_orphan_settlements(events, rng, 25, merchants)
    dup_events = inject_near_duplicates(events, rng, 100)

    augmented = events + orphan_events + dup_events

    if args.scale > 1:
        scaled = []
        for i in range(args.scale):
            for e in augmented:
                ne = copy.deepcopy(e)
                ne["event_id"] = str(uuid.uuid4())
                if i > 0:
                    # keep transactions distinct across scale copies
                    ne["transaction_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, ne["transaction_id"] + f"-{i}"))
                scaled.append(ne)
        augmented = scaled

    with open(args.output, "w") as f:
        json.dump(augmented, f, indent=1)

    print(f"input events:            {len(events)}")
    print(f"amount mismatches:       {n_mismatch}  (modified in place)")
    print(f"out-of-order pairs:      {n_ooo}  (reordered in place, timestamps unchanged)")
    print(f"orphan settlements:      {len(orphan_events)}  (new events)")
    print(f"near-duplicate replays:  {len(dup_events)}  (new events)")
    print(f"total events written:    {len(augmented)}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
