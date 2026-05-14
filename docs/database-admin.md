# DailyDigest: database admin guide

Everything DailyDigest knows is in Postgres. You can update it in two ways:

1. **By email:** send commands to your `morning@yourdomain.tld` address. See [commands.md](commands.md) for the full grammar.
2. **By SQL:** connect to Neon with `psql "$DATABASE_URL"` and run SQL.

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
```

The next digest run seeds the row again from `BIRTHDATE`, `WEATHER_LAT`, `WEATHER_LON`, and `TIMEZONE`.

## Calendars

Multiple rows, one per ICS feed.

Use email or SQL to add a calendar. Use updates to disable or re-enable it, and delete it to remove cached events through `ON DELETE CASCADE`.

```sql
SELECT id, name, enabled, ics_url FROM calendars ORDER BY id;
INSERT INTO calendars (name, ics_url) VALUES ('Work', 'https://...ics');
UPDATE calendars SET enabled = FALSE WHERE name ILIKE 'Work';
UPDATE calendars SET enabled = TRUE WHERE name ILIKE 'Work';
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

`bucket` is free-text. Only `grocery` is wired up to the dedicated `show grocery` partial today, but any bucket name appears in the rendered "(name) bucket" sub-section of the short task list.

## Long tasks

```sql
SELECT id, text, due_date FROM tasks_long ORDER BY due_date;

INSERT INTO tasks_long (text, due_date) VALUES ('M license practice exam', '2027-10-07');

DELETE FROM tasks_long WHERE text ILIKE '%M license%';
```

`due_date` is `DATE` with no time component. Long tasks use dates; short tasks can use a datetime.

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

Period values used by the parser: `half-week`, `week`, `half-month`, `month`, `half-year`, `year`. The string is for display only. `expires_at` controls auto-deletion.

## Caches

You usually do not need to touch these tables, but you can clear them to force a refresh:

```sql
DELETE FROM weather_cache;
DELETE FROM quote_cache WHERE for_date = CURRENT_DATE;
```

## Inspection

Use these queries to inspect recent processing, pending changes, logs, and cached events.

```sql
SELECT processed_at, subject, num_commands, num_errors
FROM processed_emails ORDER BY processed_at DESC LIMIT 20;
SELECT id, kind, payload, created_at
FROM pending_changes WHERE notified_at IS NULL ORDER BY id;
SELECT ts, level, message
FROM debug_log
WHERE run_id = (SELECT run_id FROM debug_log ORDER BY ts DESC LIMIT 1)
ORDER BY ts;
SELECT DISTINCT run_id, MIN(ts) AS started
FROM debug_log
WHERE level = 'ERROR'
GROUP BY run_id ORDER BY started DESC LIMIT 10;
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
