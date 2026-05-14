# DailyDigest: command reference

Send any email to `morning@yourdomain.tld` (or wherever `FROM_EMAIL` points). The script reads the inbox at the top of each hour, applies every recognized line, and replies with **one** email per run regardless of how many emails came in.

## Parsing rules

1. The parser only looks at lines that **begin with a recognized verb** (case-insensitive). Everything else, including signatures, "Warm regards", and quoted reply text, is ignored.
2. For `add`, `done`, `remove`, `delete`, `del` the payload **must be in quotes** (`"..."` or `'...'`, smart quotes accepted). Everything outside the quotes is treated as modifiers (e.g. `due ...`, `#bucket`).
3. `show` takes an unquoted keyword. Filler words like `me`, `the`, `a`, `all`, and `my` are skipped.
4. Keyword casing is irrelevant: `Add`, `aDD`, `ADD`, `+` all work.
5. Multiple emails received between two runs are aggregated into a single response email.
6. Lines starting with a verb that the parser cannot fully understand are reported in a **Could not parse** section in the response email, one line at a time with the specific reason. Everything else still executes.

Recognized verbs: `add`, `+`, `done`, `remove`, `delete`, `del`, `show`.

## Adding things

### Short-term task

```text
add "buy detergent"
+ "call dentist"
add "milk" #grocery
add "submit timesheet" due friday 5pm
```

Tags any short task with `#grocery` (or any hashtag) to put it in a bucket. The grocery bucket is the only one with a dedicated `show grocery` partial.

### Long-term task (requires a due date)

```text
add long task "M license practice exam" due 7 october 2027
add long task "tax return" due 30/4/2026
add long task "renew passport" due 2026-09-01
```

Accepted date formats (day-first interpretation):

- `7 october 2027`, `oct 7 2027`, `october 7, 2027`
- `7/10/2027` → Oct 7, 2027 (**day-first**)
- `2027-10-07`, `07-10-2027`
- Relative: `next friday`, `in 3 weeks`

### Countdown

```text
add countdown "M license" 2027-10-07
add countdown "graduation" 15 june 2026 9:00am
```

Stored by name (unique). Re-adding overwrites the target.

### Reflection

```text
add reflection for week "review React patterns"
add reflection for month "lift 3× weekly"
add reflection for half-year "ship side project"
add reflection for year "save 25k"
```

Valid periods: `half-week`, `week`, `half-month`, `month`, `half-year` (also `6 month`, `six month`), `year`. The reflection auto-clears at 11:59 PM on its expiry date.

### Calendar

```text
add calendar "Work" https://outlook.office365.com/owa/calendar/.../calendar.ics
```

Adds an ICS source to the aggregated timeline. The first calendar is seeded from the `ICS_URL` env var on first run.

## Removing things

`done`, `remove`, `delete`, and `del` are equivalent. The payload is fuzzy-matched by substring and case-insensitive, so you do not need to repeat the full task text.

```text
done "detergent"
remove "call"
done long "M license"
remove countdown "graduation"
remove reflection "React"
remove calendar "Work"
```

Notes:

- **Calendar-derived dues are not removable via `done`.** They come from the ICS feed; remove them at the source.
- `remove calendar` disables/deletes the calendar from future fetches but keeps cached events historically.

## Showing things

`show` returns a partial email containing only the requested section. Filler words are stripped.

| Command | Section |
| --- | --- |
| `show` / `show everything` / `show current` / `show me everything` | **Full digest** (every section) |
| `show calendar` / `show calendars` / `show schedule` | Today's timeline only |
| `show weather` / `show forecast` | Hourly weather, now → midnight |
| `show timetable` / `show timetables` | Calendar + weather combined |
| `show short` / `show short list` / `show list` / `show current list` / `show tasks` | Short task list |
| `show long` / `show long list` / `show long tasks` | Long task list |
| `show due` / `show dues` | Due-date dashboard (14-day window) |
| `show countdowns` | All countdowns |
| `show countdown "M license"` | Single countdown |
| `show reflection` / `show reflections` | All active reflections |
| `show quote` | Today's quote only |
| `show age` | Age + days-to-next-age only |
| `show grocery` | Grocery bucket only |

`show grocery from the long list` returns an error because long tasks do not use buckets.

## When emails are sent

Every run produces **at most one** email, decided as follows (in order):

1. **Full digest:** sent if it's 6 AM, 12 PM, or 6 PM local, or if you sent `show everything`, `show current`, or bare `show`.
2. **Partial show email:** sent if you requested any partial `show <X>` output. If the same run also has add/done/etc., a "What changed" banner is included.
3. **Tasks-updated email:** sent if there were add/done/etc. commands and no scheduled hour and no `show`. It contains the "What changed" banner plus the rest-of-day calendar and weather only.
4. **Errors-only email:** sent only if a line started with a keyword but failed to parse and nothing else happened.
5. Otherwise silent.

The 6 AM digest additionally shows your **age** (years/months/days) and how far you are from your next birthday.

## Due-date colors

Items appear in the **Due within 14 days** dashboard when their date is ≤ 14 days away. Sources:

- All long tasks
- Short tasks that have a `due_at`
- ICS events whose title contains "Due" (case-insensitive)
- ICS events whose duration ≤ 2 minutes (typical "11:59 PM → 11:59 PM" assignment slots)

Color scale:

| Days | Color |
| --- | --- |
| Overdue | dark red |
| 0–2 | red |
| 2–3 | red-orange |
| 3–5 | orange |
| 5–7 | yellow |
| 7–14 | green |

## Multiple emails per run

Send three emails between runs. The next run reads all three, applies every command across them, collects every unparseable line, and sends **one** email summarizing everything. Order is determined by Gmail's internal date.

## Debug logs

Every run writes structured log lines to stdout (visible in the GitHub Actions log) and to a `debug_log` Postgres table with a per-run UUID. The table is pruned to 7 days on every run.

To pull recent logs from psql:

```sql
SELECT ts, level, message
FROM debug_log
WHERE run_id = (SELECT run_id FROM debug_log ORDER BY ts DESC LIMIT 1)
ORDER BY ts;
```

## What was removed

- The old `clear` command no longer exists. There is no longer a "wipe the list" shortcut.
- Bullet-list-replace (sending `- a\n- b\n- c` to replace the whole list) is gone. Use `add "..."` and `done "..."` individually.
