# DailyDigest

A self-hosted daily email assistant. At 6 AM, 12 PM, and 6 PM you get a digest that combines:

- A **due-date dashboard** with traffic-light coloring (green → red as deadlines approach)
- **Hourly weather** for the rest of the day (OpenWeather One Call)
- An **aggregated timeline** of every ICS calendar you've added, with past events shown greyed-out for context
- **Short-term** and **long-term** task lists (with optional due dates, optional `#grocery` bucket)
- **Countdowns**, **reflections**, **age** (at 6 AM), and a **motivational quote** at the bottom

Tasks, calendars, countdowns, and reflections are all managed by replying to the digest. Send `add "task"`, `add long task "X" due 7 oct 2027`, `show calendar`, `done "X"`. The full command grammar is in [docs/commands.md](docs/commands.md).

The entire system runs on free tiers: a Python script in GitHub Actions, an HTTP scheduler, a transactional email API, IMAP, a Postgres database, OpenWeather, and ZenQuotes (with Quotable as fallback).

## Contents

- [Stack](#stack)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [1. Domain and inbound forwarding](#1-domain-and-inbound-forwarding)
  - [2. Outbound email service](#2-outbound-email-service)
  - [3. Gmail App Password](#3-gmail-app-password)
  - [4. Outlook calendar feed](#4-outlook-calendar-feed)
  - [5. Repository and secrets](#5-repository-and-secrets)
  - [6. Gmail filters](#6-gmail-filters)
  - [7. Hourly trigger](#7-hourly-trigger)
- [Usage](#usage)
- [Email examples](#email-examples)
- [Configuration reference](#configuration-reference)
- [Architecture notes](#architecture-notes)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Stack

| Component       | Service                    | Purpose                                                          |
| --------------- | -------------------------- | ---------------------------------------------------------------- |
| Runtime         | GitHub Actions (Ubuntu)    | Executes the Python script on demand                             |
| Scheduler       | cron-job.org               | Triggers the workflow on the hour, every hour                    |
| Language        | Python 3.12                | Script logic                                                     |
| Inbound mail    | ImprovMX                   | Forwards `*@yourdomain.tld` to a Gmail inbox                     |
| Outbound mail   | Resend                     | Sends transactional email from your verified domain              |
| Reply storage   | Gmail (IMAP)               | Holds incoming task commands until the script reads them         |
| Calendar source | Any ICS feed (multiple)    | Outlook, Google, iCloud, etc. — added via `add calendar "..." URL` |
| Weather         | OpenWeather One Call 3.0   | Hourly forecast (free tier: 1k calls/day)                        |
| Quotes          | ZenQuotes → Quotable       | Daily motivational quote, cached in DB                           |
| State           | Neon (PostgreSQL)          | Tasks, long tasks, countdowns, reflections, profile, debug log   |

All services have free tiers that comfortably cover the load (one HTTP request per hour, a handful of emails per day).

## Prerequisites

Before starting, you will need:

1. A GitHub account.
2. A domain name you control. Any registrar works.
3. A Gmail account. This receives forwarded mail and is the IMAP target.
4. An Outlook account with a calendar you want to read. Personal or work, either is fine.

The setup below walks through every external account creation.

## Setup

### 1. Domain and inbound forwarding

You need any email sent to `morning@yourdomain.tld` to land in your Gmail inbox. ImprovMX does this for free.

1. Sign up at [improvmx.com](https://improvmx.com).
2. Add your domain.
3. In your domain registrar's DNS settings, add the two MX records ImprovMX shows you. Wait for them to verify.
4. Add an alias: `morning@yourdomain.tld` forwards to your Gmail address.

You can stop here, or add more aliases. Every alias forwards to the same Gmail.

### 2. Outbound email service

You need to send email *from* `morning@yourdomain.tld`. ImprovMX does not handle outbound; Resend does.

1. Sign up at [resend.com](https://resend.com).
2. Add your domain.
3. Add the SPF (TXT) and DKIM (CNAME) records Resend shows you. Skip any MX records Resend suggests, since those conflict with ImprovMX.
4. Wait for verification (usually under five minutes).
5. Generate an API key under "API Keys". Save it.

Resend's free tier covers 3,000 emails per month. This system uses roughly 60.

### 3. Gmail App Password

The Python script reads your Gmail over IMAP. Gmail requires an App Password for this, not your regular password.

1. Open [myaccount.google.com](https://myaccount.google.com).
2. Security, then 2-Step Verification. Turn it on if it is not already.
3. Security, then App passwords. Create one named `daily-digest`.
4. Copy the 16-character password. Save it without the spaces.

### 4. Outlook calendar feed

The script reads your calendar by fetching a public ICS URL. No OAuth, no Azure app registration.

1. Open Outlook on the web.
2. Calendar, then Settings (gear icon), then "View all Outlook settings".
3. Calendar, then Shared calendars.
4. Under "Publish a calendar", choose the calendar you want and set permissions to **Can view all details**.
5. Click Publish. Copy the **ICS** URL. (Outlook also shows an HTML URL: do not use that one.)

The published feed refreshes on Outlook's side roughly every few hours. Newly added events may not appear in the digest immediately.

### 5. Repository and secrets

1. Fork or clone this repository to your GitHub account. Keep it private if you prefer.
2. Open Settings, then Secrets and variables, then Actions, then New repository secret. Add the following:

| Secret                | Value                                                                       |
| --------------------- | --------------------------------------------------------------------------- |
| `GMAIL_USER`          | Your Gmail address, e.g. `you@gmail.com`.                                   |
| `GMAIL_APP_PASSWORD`  | The 16-character App Password from step 3, no spaces.                       |
| `OWNER_EMAIL`         | Where the digest is delivered. Usually `morning@yourdomain.tld`.            |
| `FROM_EMAIL`          | The address the digest sends from, e.g. `morning@yourdomain.tld`.           |
| `RESEND_API_KEY`      | The API key from step 2.                                                    |
| `ICS_URL`             | Your first ICS feed. Used to seed the `calendars` table on first run. Additional calendars are added later via `add calendar "Name" URL`. |
| `DATABASE_URL`        | The Neon connection string (PostgreSQL). Found in your Neon project dashboard. |
| `OPENWEATHER_API_KEY` | OpenWeather One Call 3.0 key. Sign up at [openweathermap.org](https://openweathermap.org/api/one-call-3). |
| `WEATHER_LAT`         | Latitude (e.g. `48.42841`). Seeds the `profile` table on first run.         |
| `WEATHER_LON`         | Longitude (e.g. `-123.36564`). Seeds the `profile` table on first run.      |
| `BIRTHDATE`           | `YYYY-MM-DD`, e.g. `2002-06-15`. Seeds the `profile` table on first run.    |
| `FORWARD_EMAILS`      | Optional. Comma-separated extra BCC recipients. Leave blank to skip.        |

If `OWNER_EMAIL` and `FROM_EMAIL` are both on your domain (recommended), the digest header shows `to: morning@yourdomain.tld` rather than your raw Gmail address.

### 6. Gmail filters

These keep your inbox clean. They are not required for the script to work, but recommended.

1. Gmail, then Settings, then See all settings, then Filters and Blocked Addresses, then Create a new filter.
2. Filter 1, to silence outgoing digest noise:
   - **From:** `morning@yourdomain.tld`
   - Action: Skip Inbox, Mark as read, Apply label `DailyDigest/Updates`, Never mark as important.
3. Filter 2, to silence incoming command emails:
   - **To:** `morning@yourdomain.tld`
   - Action: Skip Inbox, Apply label `DailyDigest/Logs`, Never mark as important.

The script searches `[Gmail]/All Mail`, so archived emails are still found.

### 7. Hourly trigger

GitHub Actions' built-in cron is unreliable. It can run anywhere from 30 minutes to 3 hours late. cron-job.org calls the GitHub API directly and fires on the second.

Full walkthrough is in [docs/cronjob-setup.md](docs/cronjob-setup.md). Brief version:

1. Generate a GitHub fine-grained personal access token with `Actions: Read and write` on this repository.
2. Sign up at [cron-job.org](https://cron-job.org).
3. Create a job:
   - URL: `https://api.github.com/repos/<your-username>/DailyDigest/actions/workflows/daily-email.yml/dispatches`
   - Method: `POST`
   - Body: `{"ref":"main"}`
   - Headers: `Authorization: Bearer <your-PAT>`, `Content-Type: application/json`, `Accept: application/vnd.github.v3+json`
   - Schedule: every hour at `:00`.
4. Click Test run. A successful response is `204 No Content`.

After cron-job.org fires, check your Actions tab. A new run should appear within seconds.

## Usage

Send any email to `morning@yourdomain.tld` from any address. At the next hourly run, the script reads every unprocessed message, applies the commands across all of them, and replies with **exactly one** email summarizing everything.

### Commands at a glance

| Goal                          | Example                                                       |
| ----------------------------- | ------------------------------------------------------------- |
| Add a short task              | `add "buy detergent"`                                         |
| Add with a bucket             | `add "milk" #grocery`                                         |
| Add a long task (due required) | `add long task "M license exam" due 7 october 2027`          |
| Add a short task with due time | `add "submit timesheet" due friday 5pm`                      |
| Add a countdown               | `add countdown "graduation" 15 june 2026`                     |
| Add a reflection              | `add reflection for week "review React patterns"`             |
| Add another calendar          | `add calendar "Work" https://...ics`                          |
| Remove anything               | `done "detergent"` · `remove countdown "graduation"`          |
| Request the full digest now   | `show` / `show everything` / `show current`                   |
| Request one section           | `show calendar` · `show weather` · `show due` · `show grocery` |

Full reference, all aliases, date formats, and parser rules: [docs/commands.md](docs/commands.md).

The parser only acts on lines that **begin with a verb** (`add`, `+`, `done`, `remove`, `delete`, `del`, `show`). Everything else — signatures, "Warm regards", quoted reply text — is ignored silently. `add`/`done`/`remove` payloads must be in quotes (`"..."`); `show` takes an unquoted keyword.

### When emails go out

Each run produces at most **one** email, decided as:

1. **Full digest** — at 6 AM / 12 PM / 6 PM, or when you sent `show` / `show everything` / `show current`. Includes a "What changed" banner if any task/calendar/countdown/reflection commands were processed this run.
2. **Partial show email** — when you sent a specific `show <section>`. Only that section. Includes "What changed" if other commands were processed too.
3. **Tasks-updated email** — when you sent commands but didn't ask to show anything, outside of scheduled hours. Contains the "What changed" banner plus the rest-of-day calendar/weather only.
4. **Errors-only email** — only if every keyword-starting line failed to parse and nothing else happened.
5. Otherwise silent.

The 6 AM digest additionally shows your age (years/months/days) and how far you are from your next birthday.

## Email examples

The HTML sources in `docs/html/` are stale relative to the post-revamp template (sections were renamed, weather and dues added). Regenerate them by running a single end-to-end execution against a dev DB, or render directly from `templates/email.html` with a synthetic context dict.

To rasterize whatever HTML samples you do produce:

```bash
for file in *.html; do
  name="${file%.html}"
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --headless --disable-gpu --hide-scrollbars \
    --window-size=1440,2400 \
    --screenshot="${name}.png" \
    "file://$(pwd)/$file"
  sips -s format jpeg "${name}.png" --out "${name}.jpg" >/dev/null
  rm "${name}.png"
done
```

## Configuration reference

### Environment variables

All variables are passed as GitHub repository secrets and read by `scripts/digest.py`.

| Name                  | Required  | Description                                                                                       |
| --------------------- | --------- | ------------------------------------------------------------------------------------------------- |
| `GMAIL_USER`          | Yes       | Gmail address used for IMAP login.                                                                |
| `GMAIL_APP_PASSWORD`  | Yes       | 16-character App Password for the Gmail account.                                                  |
| `OWNER_EMAIL`         | Yes       | Recipient of every email the script sends.                                                        |
| `FROM_EMAIL`          | Yes       | Address the script sends from. Must be on a domain verified in Resend.                            |
| `RESEND_API_KEY`      | Yes       | Resend API key with sending permission.                                                           |
| `DATABASE_URL`        | Yes       | Neon PostgreSQL connection string.                                                                |
| `ICS_URL`             | Bootstrap | First ICS feed. Used once to seed the `calendars` table; further calendars are added by command.  |
| `OPENWEATHER_API_KEY` | Yes       | OpenWeather One Call 3.0 key. Falls back to `/data/2.5/forecast` if 3.0 returns 401.              |
| `WEATHER_LAT`         | Bootstrap | Latitude. Seeds the `profile` table on first run.                                                 |
| `WEATHER_LON`         | Bootstrap | Longitude. Seeds the `profile` table on first run.                                                |
| `BIRTHDATE`           | Bootstrap | `YYYY-MM-DD`. Seeds the `profile` table on first run. Drives age display + birthday countdown.    |
| `FORWARD_EMAILS`      | No        | Comma-separated extra BCC recipients on every outgoing email.                                     |
| `TIMEZONE`            | No        | IANA timezone string. Defaults to `America/Vancouver`.                                            |

**Bootstrap** secrets are read only when the corresponding row in Postgres is empty. After the first successful run, you can rotate `BIRTHDATE`/`WEATHER_LAT`/`WEATHER_LON`/`ICS_URL` to whatever you like (or remove them) — the DB rows are now the source of truth.

### Schedule

Defined in `.github/workflows/daily-email.yml`. The workflow has two triggers:

- `repository_dispatch` with type `cron-trigger`: used by cron-job.org.
- `workflow_dispatch`: manual trigger via the Actions UI.

The decision of *what* to send happens inside the Python script, not the workflow. See `_decide_email()` in [scripts/digest.py](scripts/digest.py). To change the digest hours:

```python
SCHEDULED_HOURS = {6, 12, 18}
```

### Customising the digest email

Edit [templates/email.html](templates/email.html). It is one HTML file with a `<style>` block; every visible block is wrapped in `{% if 'section_name' in sections %}` so the same template renders both the full digest and a partial `show calendar` email.

Section flags the renderer can pass in `sections`:

| Section name  | Renders                                  |
| ------------- | ---------------------------------------- |
| `age`         | Age line (6 AM only)                     |
| `due`         | Due-date dashboard                       |
| `weather`     | Hourly weather forecast                  |
| `calendar`    | Aggregated ICS timeline with past events |
| `short`       | Short tasks + buckets                    |
| `long`        | Long tasks                               |
| `grocery`     | Grocery bucket only (partial show)       |
| `countdowns`  | All countdowns                           |
| `countdown`   | Filtered single countdown                |
| `reflection`  | Active reflections                       |
| `quote`       | Bottom quote card                        |

The "What changed" and "Could not parse" banners render whenever `change_log`/`not_found`/`unknowns` are non-empty, regardless of `sections`.

## Architecture notes

### Why three external services instead of one?

Each service does one thing well at the free tier:

- **ImprovMX** handles inbound forwarding with MX records. It does not send.
- **Resend** sends transactional email with SPF/DKIM. It does not receive (for free).
- **Gmail** is where you read mail, and IMAP gives the script a way to find replies without setting up a webhook receiver.

You could replace any one of these. The script only depends on Gmail IMAP being reachable and Resend's HTTP API being available.

### IMAP search query

The script searches `[Gmail]/All Mail` with Gmail's native `X-GM-RAW` syntax:

```
deliveredto:morning@yourdomain.tld newer_than:2d
```

`deliveredto:` matches the `Delivered-To` header that ImprovMX adds when forwarding. This works regardless of who sent the original email, so you can email `morning@yourdomain.tld` from any account and the script will find the message.

Already-processed messages are flagged `\Seen` after handling. Future runs skip them.

### Recurring calendar events

`recurring-ical-events` expands ICS `RRULE` patterns into individual occurrences. Without this library, weekly recurring meetings would not appear in the digest because their `DTSTART` is in the past.

Past events are **kept** in the timeline now, but rendered with a `.past` CSS class (strike-through time, greyed-out title) so you can see what's already happened today in the same chronological order. Old behavior (silently filtering past events) was removed in the revamp.

### State persistence

Everything is in Neon Postgres. The schema (created idempotently by `db.run_migrations` on every run):

- `profile` — single-row config: birthdate, weather lat/lon, timezone
- `calendars` — every ICS feed (the original `ICS_URL` seeds the first row)
- `tasks_short`, `tasks_long` — the two task lists (old `tasks` table is auto-migrated into `tasks_short` on first run, then dropped)
- `countdowns`, `reflections` — named timers and time-limited reflections
- `events_cache` — last-fetched events per calendar (debug / cross-checking)
- `weather_cache`, `quote_cache` — daily-or-shorter caches for the external APIs
- `processed_emails` — every Gmail message UID we have already acted on (dedupe across `\Seen` failures)
- `pending_changes` — every command applied this run, marked `notified_at` once the outbound email is sent
- `debug_log` — per-run UUID + timestamped log lines, 7-day retention

No git commits are made for state changes; everything lives in the DB.

### Debugging a bad run

Every email and every command path writes to `debug_log` with the same `run_id` (a UUID generated at the top of `main()`). To see the latest run:

```sql
SELECT ts, level, message
FROM debug_log
WHERE run_id = (SELECT run_id FROM debug_log ORDER BY ts DESC LIMIT 1)
ORDER BY ts;
```

The same lines are also `print()`-ed to stdout, so they're visible in the GitHub Actions run log.

## Development

### File structure

```text
.
├── .github/workflows/
│   ├── daily-email.yml         Main workflow, triggered hourly via cron-job.org.
│   └── cleanup.yml             Deletes daily-email runs older than 2 days.
├── docs/
│   ├── commands.md             Full command grammar + parser rules.
│   ├── cronjob-setup.md        cron-job.org configuration walkthrough.
│   ├── html/                   Sample rendered emails (regenerate after revamp).
│   └── screenshots/            Screenshots for README.
├── scripts/
│   ├── digest.py               Entrypoint + run decision tree.
│   ├── db.py                   Postgres schema + every DB I/O function.
│   ├── parser.py               Command grammar; pure function.
│   ├── fetchers.py             ICS aggregation, OpenWeather, quote fallback.
│   ├── imap_inbox.py           IMAP read + DB-backed dedupe.
│   ├── render.py               Jinja + premailer + section shaping helpers.
│   └── sender.py               Resend HTTP wrapper.
├── templates/
│   └── email.html              One template; sections gated by `{% if X in sections %}`.
├── requirements.txt
├── LICENSE
└── README.md
```

### Local testing

To run the script locally:

```bash
git clone https://github.com/<your-username>/DailyDigest.git
cd DailyDigest
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (it is in `.gitignore`) with the same variables you set as GitHub secrets, then:

```bash
set -a; source .env; set +a
python scripts/digest.py
```

The script writes every step both to stdout (visible in GitHub Actions) and to the `debug_log` table (queryable later). A run that finds nothing to do logs roughly:

```text
[INFO] === run start 6f2a8c... at 2026-05-14T14:00:01-07:00 ===
[INFO] IMAP connect you@gmail.com
[INFO] select All Mail: OK
[INFO] IMAP returned 0 message(s)
[INFO] IMAP unprocessed: 0
[INFO] decision: kind=None sections=[]
[INFO] nothing to send; exiting
[INFO] === run end 6f2a8c... ===
```

A run that processes a `show calendar` email at 2 PM logs:

```text
[INFO] IMAP unprocessed: 1
[INFO] decision: kind=partial sections=['calendar']
[INFO] fetching ICS: Primary (https://outlook.office365.com/owa/...)
[INFO] calendars: 1, total events today: 5
[INFO] Resend sent id=abc-123 subject='Show: calendar — sat may 14 2:00 pm'
```

### Modifying behaviour

| Goal                                       | File                  | Where                                    |
| ------------------------------------------ | --------------------- | ---------------------------------------- |
| Change digest send times                   | `scripts/digest.py`   | `SCHEDULED_HOURS = {6, 12, 18}`          |
| Add a new command verb                     | `scripts/parser.py`   | extend `VERBS` + add a handler           |
| Add a new section to the digest            | `templates/email.html`+ `scripts/digest.py` | `_build_context` / `_full_sections` |
| Change due-color thresholds                | `scripts/render.py`   | `_color_for_days`                        |
| Change weather cache lifetime              | `scripts/fetchers.py` | `db.weather_cache_get_fresh(...)` arg    |
| Change quote retention                     | `scripts/fetchers.py` | `fetch_quote`                            |
| Change IMAP search window                  | `scripts/imap_inbox.py` | the `newer_than:Nd` literal in `query` |

## Troubleshooting

**Workflow runs but no email arrives.** Check the Actions run log. Common causes: invalid Gmail App Password (must be 16 characters with no spaces), Resend API key revoked, `FROM_EMAIL` not on a verified Resend domain.

**Task command not picked up.** Verify the email landed in Gmail by checking `[Gmail]/All Mail` for the address `morning@yourdomain.tld`. If it is there, check the run log for the IMAP search output. If the search returned 0 messages, the `Delivered-To` header may not match `FROM_EMAIL`.

**cron-job.org returns 404.** The PAT does not have access to the repository, or the URL is wrong. Confirm the token has `Actions: Read and write` on the specific repo and the URL spells the repo name correctly.

**cron-job.org returns 401.** The `Authorization` header is malformed. The value must be exactly `Bearer <token>` with a space after `Bearer`.

**Calendar empty when it should not be.** Outlook publishes the ICS feed on its own schedule (every few hours). Newly added events may not appear until Outlook refreshes the feed. Also verify the ICS URL works in a browser.

**Workflow run history fills up.** The `cleanup.yml` workflow runs daily and deletes `daily-email.yml` runs older than 2 days. If it stops working, check the Actions tab for failed cleanup runs.

## License

Apache License with attribution. See [LICENSE](LICENSE). You can use this code in personal or commercial projects, modify it, and redistribute it.
