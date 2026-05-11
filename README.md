# Daily Digest

A GitHub Actions workflow that emails you a styled daily digest every morning — your Outlook calendar events for the day plus a to-do list. Reply to the email to edit your tasks.

## How it works

Each morning at 6 AM Pacific the workflow:

1. Logs into a Gmail inbox via IMAP and reads any unread replies from you
2. Parses them as commands (`add: X`, `done: X`, `clear`, or a bullet list) and updates `tasks.md`
3. Commits the change back to the repo so it persists for next time
4. Fetches today's events from your published Outlook calendar (ICS feed)
5. Renders a clean HTML email and sends it via Gmail SMTP

Everything stays inside your GitHub repo — no servers, no databases.

## Setup (one-time)

### 1. Create a dedicated Gmail account

This account both sends the digest *and* receives your reply-commands. Easiest with a fresh Gmail just for this — e.g. `yourname.digest@gmail.com`.

1. Create the account
2. Enable 2-Step Verification on it
3. Generate an **App Password**: <https://myaccount.google.com/apppasswords>
4. Save the 16-character password (you'll paste it as a secret in step 4)

### 2. Publish your Outlook calendar as ICS

1. Outlook on the web → **Calendar**
2. Settings (gear icon) → **View all Outlook settings**
3. **Calendar** → **Shared calendars**
4. Under "Publish a calendar", pick your calendar, set permissions to **Can view all details**, click **Publish**
5. Copy the **ICS** link (the `.ics` URL — not the HTML one)

Note: Outlook refreshes the published feed roughly every few hours. Meetings added between feed refreshes won't appear in the digest until the next refresh.

### 3. Create the repo

Push these files to a new GitHub repo (private is fine).

### 4. Add repository secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Value |
|---|---|
| `GMAIL_USER` | The dedicated Gmail address (e.g. `yourname.digest@gmail.com`) |
| `GMAIL_APP_PASSWORD` | The 16-char app password from step 1 (no spaces) |
| `OWNER_EMAIL` | The email *you* read mail at and reply from — the digest goes here |
| `ICS_URL` | The Outlook `.ics` URL from step 2 |

### 5. Adjust the send time

In `.github/workflows/daily-email.yml`, the cron is in UTC:

- `0 13 * * *` → 6 AM PDT (Mar–Nov) / 5 AM PST (Nov–Mar)
- `0 14 * * *` → 7 AM PDT / 6 AM PST

Pick whichever you prefer and adjust if you want a different time. GitHub cron can run 5–15 minutes late, so don't use this for time-critical reminders.

### 6. Test

In the Actions tab, open **Daily Digest** and click **Run workflow**. Check your inbox.

## Using it

### Editing tasks by email

Just reply to the digest with one of:

- `add: walk the dog` — append a task
- `done: walk the dog` — remove the first matching task (case-insensitive substring match)
- `clear` — empty the list

Or send a fresh bullet list to replace everything:

```
- buy milk
- call mom
- finish report
```

The command must be at the top of the email body. Quoted reply text below is ignored automatically.

### Editing directly

You can also just edit `tasks.md` in GitHub and commit. The next run will use the new list.

## Customizing the email

The template is `templates/email.html`. It's a single HTML file with a `<style>` block; the script inlines the CSS automatically (with `premailer`) before sending so it renders correctly in Gmail, Outlook, Apple Mail, etc.

Available Jinja2 variables:

- `weekday` — e.g. `Monday`
- `date_long` — e.g. `May 11, 2026`
- `events` — list of `{time, title}` dicts
- `tasks` — list of strings

## Costs

| Service | Cost |
|---|---|
| GitHub Actions | Free (this uses ~1 min/day of the 2,000 free monthly minutes) |
| Gmail | Free |
| **Total** | **Free** |

## Possible upgrades later

- **Send from your own domain**: swap Gmail SMTP for Resend or Postmark. Cleaner sender. You'd still want a Gmail (or similar IMAP-capable inbox) to receive replies — or use the providers' inbound-parse webhook to a Cloudflare Worker.
- **Real-time calendar**: switch ICS for the Microsoft Graph API. Removes the ~3-hour feed lag but requires an Azure app registration and OAuth refresh-token plumbing.
- **More commands**: extend `parse_command()` in `scripts/digest.py` — e.g. priorities, due dates, sections.

## Troubleshooting

- **No email arrived**: check the Actions tab for the run log. Common: wrong app password (must be 16 chars, no spaces), 2FA not enabled on the Gmail account, secrets not saved.
- **Reply didn't take effect**: confirm the reply was sent *from* the `OWNER_EMAIL` address (not a forward), and that the command is on the first non-empty line.
- **Calendar empty when it shouldn't be**: confirm the ICS URL works in a browser, and that your timezone (`TIMEZONE` env in the workflow YAML) is correct.
- **Permission denied on push**: confirm `permissions: contents: write` is set in the workflow (it is by default in this file).
