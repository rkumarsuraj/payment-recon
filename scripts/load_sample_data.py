#!/usr/bin/env python3
"""Load sample_events_augmented.json through POST /events, in batches.

This deliberately goes through the HTTP API (not a direct DB insert) so
the load itself exercises and times the real ingestion path, including
idempotency. Running it twice is the idempotency demo: the second run
should report `duplicates` == total events, `inserted` == 0.

Usage:
    python scripts/load_sample_data.py --base-url http://localhost:8000 \
        --file sample_events_augmented.json --batch-size 500
"""
import argparse
import json
import time

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--file", default="sample_events_augmented.json")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    with open(args.file) as f:
        events = json.load(f)

    total_inserted = total_duplicates = total_rejected = 0
    start = time.time()

    with httpx.Client(timeout=30.0) as client:
        for i in range(0, len(events), args.batch_size):
            batch = events[i : i + args.batch_size]
            resp = client.post(f"{args.base_url}/events", json={"events": batch})
            resp.raise_for_status()
            body = resp.json()
            total_inserted += body["inserted"]
            total_duplicates += body["duplicates"]
            total_rejected += body["rejected"]
            print(
                f"batch {i // args.batch_size + 1}: "
                f"received={body['received']} inserted={body['inserted']} "
                f"duplicates={body['duplicates']} rejected={body['rejected']}"
            )

    elapsed = time.time() - start
    print("---")
    print(f"total events in file: {len(events)}")
    print(f"inserted:   {total_inserted}")
    print(f"duplicates: {total_duplicates}")
    print(f"rejected:   {total_rejected}")
    print(f"elapsed:    {elapsed:.1f}s")


if __name__ == "__main__":
    main()
