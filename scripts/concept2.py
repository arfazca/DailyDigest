"""Concept2 Logbook API client — personal access token mode.

Set CONCEPT2_TOKEN in your .env (get it from
https://log.concept2.com/profile → "Your Access Token").
No OAuth setup needed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import db


API_BASE = "https://log.concept2.com/api"


def get_token() -> str | None:
    return os.environ.get("CONCEPT2_TOKEN")


def _normalize_result(raw: dict) -> dict:
    hr = raw.get("heart_rate") or {}
    return {
        "id": raw["id"],
        "date": raw.get("date"),
        "type": raw.get("type"),
        "workout_type": raw.get("workout_type"),
        "distance": raw.get("distance") or 0,
        "time_tenths": raw.get("time") or 0,
        "time_formatted": raw.get("time_formatted") or "",
        "stroke_rate": raw.get("stroke_rate"),
        "heart_rate_avg": hr.get("average") if isinstance(hr, dict) else None,
        "calories_total": raw.get("calories_total"),
        "drag_factor": raw.get("drag_factor"),
        "comments": raw.get("comments"),
        "raw": raw,
    }


def fetch_all_results(conn, token: str,
                       updated_after: datetime | None = None) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict] = []
    page = 1
    while True:
        params: dict = {"page": page, "number": 250}
        if updated_after is not None:
            params["updated_after"] = updated_after.strftime("%Y-%m-%dT%H:%M:%S")
        r = requests.get(f"{API_BASE}/users/me/results", headers=headers,
                           params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        for raw in (body.get("data") or []):
            try:
                out.append(_normalize_result(raw))
            except Exception as exc:
                db.log(conn, "WARN", f"concept2: skipped result: {exc}")
        meta = (body.get("meta") or {}).get("pagination") or {}
        if page >= (meta.get("total_pages") or 1):
            break
        page += 1
    return out


def sync_results(conn, full_resync: bool = False) -> int:
    token = get_token()
    if not token:
        db.log(conn, "WARN", "concept2: CONCEPT2_TOKEN not set; skipping")
        return 0
    since: datetime | None = None
    if not full_resync:
        last = db.concept2_last_sync_at(conn)
        if last:
            since = last - timedelta(hours=1)
    try:
        results = fetch_all_results(conn, token, updated_after=since)
    except Exception as exc:
        db.log(conn, "ERROR", f"concept2: fetch failed: {exc}")
        return 0
    if not results:
        return 0
    written = db.concept2_results_upsert_many(conn, results)
    db.log(conn, "INFO",
            f"concept2: synced {len(results)} results ({written} rows)")
    return len(results)
