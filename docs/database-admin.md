# DailyDigest — database admin guide

Everything DailyDigest knows is in Postgres. You can manipulate any of it two ways:

1. **By email** (preferred): send commands to your `morning@yourdomain.tld` address. See [commands.md](commands.md) for the full grammar.
2. **By SQL** (direct): connect to Neon with `psql "$DATABASE_URL"` and run SQL.

This file lists the SQL form for every common admin operation.

## Connect

```bash
psql "$DATABASE_URL"
```

Or one-shot:

```bash
psql "$DATABASE_URL" -c "SELECT * FROM profile;"
```

## Profile

One row, `id = 1`. Holds your birthdate, weather lat/lon, timezone.

```sql
SELECT * FROM profile;

UPDATE profile SET birthdate    = '2002-06-15'  WHERE id = 1;
UPDATE profile SET weather_lat  = 48.42841      WHERE id = 1;
UPDATE profile SET weather_lon  = -123.36564    WHERE id = 1;
UPDATE profile SET timezone     = 'America/Vancouver' WHERE id = 1;
```

To re-bootstrap from env vars on next run, delete the row:

```sql
DELETE FROM profile WHERE id = 1;
-- Next digest run re-seeds from BIRTHDATE / WEATHER_LAT / WEATHER_LON / TIMEZONE
```

## Calendars

Multiple rows, one per ICS feed.

```sql
SELECT id, name, enabled, ics_url FROM calendars ORDER BY id;

-- Add a calendar (or via email: add calendar "Work" https://...ics)
INSERT INTO calendars (name, ics_url) VALUES ('Work', 'https://...ics');

-- Disable without losing the row
UPDATE calendars SET enabled = FALSE WHERE name ILIKE 'Work';

-- Re-enable
UPDATE calendars SET enabled = TRUE WHERE name ILIKE 'Work';

-- Hard delete (also drops cached events via ON DELETE CASCADE)
DELETE FROM calendars WHERE name ILIKE 'Work';
```

## Short tasks

```sql
SELECT id, text, bucket, due_at FROM tasks_short ORDER BY id;

INSERT INTO tasks_short (text, bucket, due_at)
VALUES ('milk', 'grocery', NULL);

INSERT INTO tasks_short (text, due_at)
VALUES ('submit timesheet', '2026-05-16 17:00 America/Vancouver');

DELETE FROM tasks_short WHERE text ILIKE '%detergent%';
```

`bucket` is free-text. Only `grocery` is wired up to the dedicated `show grocery` partial today, but any bucket name appears in the rendered "<name> bucket" sub-section of the short task list.

## Long tasks

```sql
SELECT id, text, due_date FROM tasks_long ORDER BY due_date;

INSERT INTO tasks_long (text, due_date) VALUES ('M license practice exam', '2027-10-07');

DELETE FROM tasks_long WHERE text ILIKE '%M license%';
```

`due_date` is `DATE`, no time component (per the original spec — long tasks have dates, short tasks can have a datetime).

## Countdowns

```sql
SELECT id, name, target_datetime FROM countdowns ORDER BY target_datetime;

INSERT INTO countdowns (name, target_datetime)
VALUES ('graduation', '2026-06-15 09:00 America/Vancouver')
ON CONFLICT (name) DO UPDATE SET target_datetime = EXCLUDED.target_datetime;

DELETE FROM countdowns WHERE name ILIKE '%graduation%';
```

`name` is unique. Re-inserting overwrites the target.

## Reflections

Auto-expiring notes; auto-pruned when the digest runs after `expires_at`.

```sql
SELECT id, text, period, expires_at FROM reflections ORDER BY expires_at;

INSERT INTO reflections (text, period, expires_at)
VALUES ('lift 3× weekly', 'month',
        (CURRENT_DATE + INTERVAL '30 days')::date + TIME '23:59');

DELETE FROM reflections WHERE text ILIKE '%lift%';
```

Period values used by the parser: `half-week`, `week`, `half-month`, `month`, `half-year`, `year`. The string is only display-decoration — `expires_at` is what governs auto-deletion.

## Caches

You almost never need to touch these — but if you want to force a refresh:

```sql
-- Force a weather refetch on next run that needs weather
DELETE FROM weather_cache;

-- Force a quote refetch on next run (or wait for 6 PM, the natural refresh)
DELETE FROM quote_cache WHERE for_date = CURRENT_DATE;
```

## Inspection

```sql
-- All emails ever processed (and how many commands per email)
SELECT processed_at, subject, num_commands, num_errors
FROM processed_emails ORDER BY processed_at DESC LIMIT 20;

-- Pending changes that haven't been emailed out yet
SELECT id, kind, payload, created_at
FROM pending_changes WHERE notified_at IS NULL ORDER BY id;

-- Latest GitHub Actions run logs (per-run UUID)
SELECT ts, level, message
FROM debug_log
WHERE run_id = (SELECT run_id FROM debug_log ORDER BY ts DESC LIMIT 1)
ORDER BY ts;

-- Find runs that emitted ERRORs
SELECT DISTINCT run_id, MIN(ts) AS started
FROM debug_log
WHERE level = 'ERROR'
GROUP BY run_id ORDER BY started DESC LIMIT 10;

-- Most recent events cached per calendar
SELECT c.name, COUNT(*) AS n, MAX(ec.fetched_at) AS last_fetched
FROM events_cache ec JOIN calendars c ON c.id = ec.calendar_id
GROUP BY c.name;
```

## Nuke and start over

If you want to wipe everything to a clean slate and let the next run re-seed from env vars:

```sql
TRUNCATE
  tasks_short, tasks_long, countdowns, reflections,
  pending_changes, processed_emails,
  events_cache, weather_cache, quote_cache,
  debug_log, calendars, profile
RESTART IDENTITY CASCADE;
```

The next run will recreate the schema (already exists, no-op), then seed `profile` from `BIRTHDATE`/`WEATHER_LAT`/`WEATHER_LON` and `calendars` from `ICS_URL`.
