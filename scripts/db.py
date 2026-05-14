"""Postgres schema + queries for DailyDigest.

Every table is created idempotently on each run by `run_migrations`. The
existing `tasks` table is migrated into `tasks_short` on first run and
seeded values are taken from environment variables only when their DB
counterparts are empty.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import Json, RealDictCursor


RUN_ID = uuid.uuid4().hex


def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def run_migrations(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS profile (
            id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            birthdate    DATE,
            weather_lat  NUMERIC(8,5),
            weather_lon  NUMERIC(8,5),
            timezone     TEXT
        );

        CREATE TABLE IF NOT EXISTS calendars (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            ics_url     TEXT NOT NULL UNIQUE,
            enabled     BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS tasks_short (
            id          SERIAL PRIMARY KEY,
            text        TEXT NOT NULL,
            bucket      TEXT,
            due_at      TIMESTAMPTZ,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS tasks_long (
            id          SERIAL PRIMARY KEY,
            text        TEXT NOT NULL,
            due_date    DATE NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS countdowns (
            id              SERIAL PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            target_datetime TIMESTAMPTZ NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS reflections (
            id          SERIAL PRIMARY KEY,
            text        TEXT NOT NULL,
            period      TEXT NOT NULL,
            set_at      TIMESTAMPTZ DEFAULT NOW(),
            expires_at  TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weather_cache (
            id         SERIAL PRIMARY KEY,
            fetched_at TIMESTAMPTZ NOT NULL,
            payload    JSONB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quote_cache (
            for_date DATE PRIMARY KEY,
            text     TEXT NOT NULL,
            author   TEXT,
            source   TEXT
        );

        CREATE TABLE IF NOT EXISTS events_cache (
            id          SERIAL PRIMARY KEY,
            calendar_id INT REFERENCES calendars(id) ON DELETE CASCADE,
            uid         TEXT,
            summary     TEXT,
            dtstart     TIMESTAMPTZ,
            dtend       TIMESTAMPTZ,
            is_all_day  BOOLEAN,
            fetched_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS events_cache_dtstart_idx
            ON events_cache(dtstart);

        CREATE TABLE IF NOT EXISTS processed_emails (
            gmail_msg_id TEXT PRIMARY KEY,
            processed_at TIMESTAMPTZ DEFAULT NOW(),
            num_commands INT,
            num_errors   INT,
            subject      TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_changes (
            id          SERIAL PRIMARY KEY,
            kind        TEXT NOT NULL,
            payload     JSONB NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            notified_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS debug_log (
            id      SERIAL PRIMARY KEY,
            run_id  TEXT NOT NULL,
            ts      TIMESTAMPTZ DEFAULT NOW(),
            level   TEXT,
            message TEXT
        );
        CREATE INDEX IF NOT EXISTS debug_log_run_idx
            ON debug_log(run_id, ts);
        """)

        cur.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name='tasks' AND table_schema='public'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='tasks_short' AND column_name='id'
                  AND table_schema='public'
                LIMIT 1
            ) THEN
                NULL;
            END IF;
        END $$;
        """)

        cur.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name='tasks' AND table_schema='public'
            ) THEN
                INSERT INTO tasks_short (text)
                SELECT text FROM tasks
                WHERE NOT EXISTS (
                    SELECT 1 FROM tasks_short ts WHERE ts.text = tasks.text
                );
                DROP TABLE tasks;
            END IF;
        END $$;
        """)
    conn.commit()


def seed_from_env(conn) -> None:
    """One-time seed of profile + calendars from env vars if DB is empty."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM profile WHERE id = 1")
        if cur.fetchone() is None:
            tz = os.environ.get("TIMEZONE") or "America/Vancouver"
            cur.execute(
                "INSERT INTO profile (id, timezone) VALUES (1, %s)",
                (tz,),
            )

        cur.execute("SELECT birthdate, weather_lat, weather_lon FROM profile WHERE id=1")
        bd, lat, lon = cur.fetchone()

        if bd is None and os.environ.get("BIRTHDATE"):
            cur.execute(
                "UPDATE profile SET birthdate = %s WHERE id=1",
                (os.environ["BIRTHDATE"],),
            )

        if lat is None and os.environ.get("WEATHER_LAT") and os.environ.get("WEATHER_LON"):
            cur.execute(
                "UPDATE profile SET weather_lat=%s, weather_lon=%s WHERE id=1",
                (os.environ["WEATHER_LAT"], os.environ["WEATHER_LON"]),
            )

        cur.execute("SELECT COUNT(*) FROM calendars")
        (n,) = cur.fetchone()
        if n == 0 and os.environ.get("ICS_URL"):
            cur.execute(
                "INSERT INTO calendars (name, ics_url) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                ("Primary", os.environ["ICS_URL"]),
            )
    conn.commit()


def log(conn, level: str, message: str) -> None:
    print(f"[{level}] {message}", flush=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO debug_log (run_id, level, message) VALUES (%s, %s, %s)",
                (RUN_ID, level, message),
            )
        conn.commit()
    except Exception as exc:
        print(f"[WARN] debug_log insert failed: {exc}", flush=True)


def prune_debug_log(conn, days: int = 7) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM debug_log WHERE ts < NOW() - %s::interval",
            (f"{days} days",),
        )
    conn.commit()


def get_profile(conn) -> dict:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM profile WHERE id = 1")
        row = cur.fetchone()
    return dict(row) if row else {}


def list_calendars(conn, only_enabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM calendars" + (" WHERE enabled" if only_enabled else "") + " ORDER BY id"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def add_calendar(conn, name: str, url: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO calendars (name, ics_url) VALUES (%s, %s) "
            "ON CONFLICT (ics_url) DO UPDATE SET name = EXCLUDED.name, enabled = TRUE "
            "RETURNING id",
            (name, url),
        )
        (cid,) = cur.fetchone()
    conn.commit()
    return cid


def remove_calendar_by_name(conn, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM calendars WHERE name ILIKE %s", (name,))
        n = cur.rowcount
    conn.commit()
    return n


def short_tasks(conn, bucket: str | None = None) -> list[dict]:
    sql = "SELECT * FROM tasks_short"
    params: tuple = ()
    if bucket is not None:
        sql += " WHERE bucket = %s"
        params = (bucket,)
    sql += " ORDER BY id"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def short_task_exists(conn, text: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tasks_short WHERE LOWER(text) = LOWER(%s)", (text,))
        return cur.fetchone() is not None


def add_short_task(conn, text: str, bucket: str | None, due_at: datetime | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks_short (text, bucket, due_at) VALUES (%s, %s, %s) RETURNING id",
            (text, bucket, due_at),
        )
        (tid,) = cur.fetchone()
    conn.commit()
    return tid


def remove_short_task(conn, substring: str) -> dict | None:
    """Remove the first short task whose text contains substring (case-insensitive)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM tasks_short WHERE text ILIKE %s ORDER BY id LIMIT 1",
            (f"%{substring}%",),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("DELETE FROM tasks_short WHERE id = %s", (row["id"],))
    conn.commit()
    return dict(row)


def long_tasks(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasks_long ORDER BY due_date, id")
        return [dict(r) for r in cur.fetchall()]


def add_long_task(conn, text: str, due_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks_long (text, due_date) VALUES (%s, %s) RETURNING id",
            (text, due_date),
        )
        (tid,) = cur.fetchone()
    conn.commit()
    return tid


def remove_long_task(conn, substring: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM tasks_long WHERE text ILIKE %s ORDER BY due_date, id LIMIT 1",
            (f"%{substring}%",),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("DELETE FROM tasks_long WHERE id = %s", (row["id"],))
    conn.commit()
    return dict(row)


def countdowns(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM countdowns ORDER BY target_datetime")
        return [dict(r) for r in cur.fetchall()]


def get_countdown(conn, name_substr: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM countdowns WHERE name ILIKE %s ORDER BY target_datetime LIMIT 1",
            (f"%{name_substr}%",),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def add_countdown(conn, name: str, target: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO countdowns (name, target_datetime) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET target_datetime = EXCLUDED.target_datetime "
            "RETURNING id",
            (name, target),
        )
        (cid,) = cur.fetchone()
    conn.commit()
    return cid


def remove_countdown(conn, name_substr: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM countdowns WHERE name ILIKE %s ORDER BY id LIMIT 1",
            (f"%{name_substr}%",),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("DELETE FROM countdowns WHERE id = %s", (row["id"],))
    conn.commit()
    return dict(row)


def reflections(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("DELETE FROM reflections WHERE expires_at < NOW()")
        cur.execute("SELECT * FROM reflections ORDER BY expires_at")
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def add_reflection(conn, text: str, period: str, expires_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reflections (text, period, expires_at) VALUES (%s, %s, %s) RETURNING id",
            (text, period, expires_at),
        )
        (rid,) = cur.fetchone()
    conn.commit()
    return rid


def remove_reflection(conn, substring: str) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM reflections WHERE text ILIKE %s ORDER BY id LIMIT 1",
            (f"%{substring}%",),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("DELETE FROM reflections WHERE id = %s", (row["id"],))
    conn.commit()
    return dict(row)


def set_profile_field(conn, field: str, value: Any) -> None:
    if field not in {"birthdate", "weather_lat", "weather_lon", "timezone"}:
        raise ValueError(f"unknown profile field: {field}")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO profile (id) VALUES (1) ON CONFLICT DO NOTHING")
        cur.execute(f"UPDATE profile SET {field} = %s WHERE id = 1", (value,))
    conn.commit()


def replace_events_cache(conn, calendar_id: int, rows: Iterable[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM events_cache WHERE calendar_id = %s", (calendar_id,))
        for r in rows:
            cur.execute(
                "INSERT INTO events_cache "
                "(calendar_id, uid, summary, dtstart, dtend, is_all_day) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (calendar_id, r.get("uid"), r["summary"], r["dtstart"],
                 r.get("dtend"), r.get("is_all_day", False)),
            )
    conn.commit()


def events_for_window(conn, start: datetime, end: datetime) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT ec.*, c.name AS calendar_name "
            "FROM events_cache ec LEFT JOIN calendars c ON c.id = ec.calendar_id "
            "WHERE ec.dtstart >= %s AND ec.dtstart < %s "
            "ORDER BY is_all_day DESC, ec.dtstart",
            (start, end),
        )
        return [dict(r) for r in cur.fetchall()]


def weather_cache_get_fresh(conn, max_age_minutes: int = 30) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM weather_cache "
            "WHERE fetched_at > NOW() - %s::interval "
            "ORDER BY fetched_at DESC LIMIT 1",
            (f"{max_age_minutes} minutes",),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def weather_cache_put(conn, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM weather_cache WHERE fetched_at < NOW() - INTERVAL '24 hours'")
        cur.execute(
            "INSERT INTO weather_cache (fetched_at, payload) VALUES (NOW(), %s)",
            (Json(payload),),
        )
    conn.commit()


def quote_cache_get(conn, for_date: date) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM quote_cache WHERE for_date = %s", (for_date,))
        row = cur.fetchone()
    return dict(row) if row else None


def quote_cache_put(conn, for_date: date, text: str, author: str | None, source: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO quote_cache (for_date, text, author, source) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (for_date) DO UPDATE SET "
            "text = EXCLUDED.text, author = EXCLUDED.author, source = EXCLUDED.source",
            (for_date, text, author, source),
        )
    conn.commit()


def email_already_processed(conn, gmail_msg_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM processed_emails WHERE gmail_msg_id = %s",
            (gmail_msg_id,),
        )
        return cur.fetchone() is not None


def mark_email_processed(
    conn, gmail_msg_id: str, subject: str, num_commands: int, num_errors: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_emails "
            "(gmail_msg_id, subject, num_commands, num_errors) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (gmail_msg_id) DO NOTHING",
            (gmail_msg_id, subject, num_commands, num_errors),
        )
    conn.commit()


def add_pending_change(conn, kind: str, payload: dict) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_changes (kind, payload) VALUES (%s, %s) RETURNING id",
            (kind, Json(payload)),
        )
        (pid,) = cur.fetchone()
    conn.commit()
    return pid


def pending_changes_unnotified(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM pending_changes WHERE notified_at IS NULL ORDER BY id"
        )
        return [dict(r) for r in cur.fetchall()]


def mark_pending_changes_notified(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_changes SET notified_at = NOW() WHERE notified_at IS NULL"
        )
        return cur.rowcount


def prune_old_pending(conn, days: int = 7) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pending_changes "
            "WHERE notified_at IS NOT NULL AND notified_at < NOW() - %s::interval",
            (f"{days} days",),
        )
    conn.commit()
