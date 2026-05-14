#!/usr/bin/env python3
"""Test Concept2 API token and do a full sync of your results.

Set CONCEPT2_TOKEN in your .env, then run:
    python scripts/setup_concept2.py

Get your token at: https://log.concept2.com/profile  (scroll to "Your Access Token")
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import concept2
import db


def main() -> int:
    token = concept2.get_token()
    if not token:
        print("Error: CONCEPT2_TOKEN is not set in your environment.")
        print("Get your token at https://log.concept2.com/profile")
        print('Then add to .env: CONCEPT2_TOKEN=your_token_here')
        return 1

    print(f"Token found (length {len(token)}). Testing API...")
    try:
        r = __import__("requests").get(
            f"{concept2.API_BASE}/users/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code == 401:
            print("Error: Token rejected (401). Check that CONCEPT2_TOKEN is correct.")
            return 1
        r.raise_for_status()
        user = r.json().get("data") or r.json()
        print(f"Authenticated as: {user.get('username') or user.get('email') or 'unknown'}")
    except Exception as exc:
        print(f"API test failed: {exc}")
        return 1

    conn = db.connect()
    db.run_migrations(conn)
    print("Performing full sync of all your results...")
    n = concept2.sync_results(conn, full_resync=True)
    print(f"Synced {n} results.")

    totals = db.concept2_lifetime_totals(conn)
    dist_km = round(int(totals.get("distance") or 0) / 1000, 1)
    print(f"Lifetime in DB: {totals.get('n') or 0} workouts, {dist_km} km")
    conn.close()
    print("Done. Concept2 section will appear in the next digest run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
