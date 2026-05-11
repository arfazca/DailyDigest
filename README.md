# Daily Digest

A fully automated daily email digest — your Outlook calendar events and a to-do list, delivered at 6 AM every morning. Reply (or email) to edit your tasks from any device, any email address.

## How it works

**Sending:** cron-job.org fires an HTTP request to the GitHub API every hour on the dot. GitHub Actions runs the workflow.

**At 6 AM:** the script fetches your Outlook calendar and tasks, renders a styled HTML email, and sends it via Resend from your custom domain.

**Any other hour:** the script only checks for new task commands in your Gmail inbox (via IMAP) and replies immediately if anything changed.

**Editing tasks:** send any email to `morning@arfaz.ca` with commands like `add: X` or `done: X`. It arrives in your Gmail (via ImprovMX), gets picked up on the next hourly run (within 60 minutes), and you get a reply showing exactly what changed.

## Setup

### 1. ImprovMX (already done)
`morning@arfaz.ca` forwards to your Gmail. No changes needed.

### 2. Resend (already done)
Domain `arfaz.ca` verified. Grab your API key from resend.com/api-keys.

### 3. Gmail App Password
1. Enable 2-Step Verification at myaccount.google.com
2. Go to myaccount.google.com/apppasswords
3. Create one called `daily-digest`
4. Save the 16-character password

### 4. Publish your Outlook calendar as ICS
1. Outlook web → Calendar → Settings (gear) → View all Outlook settings
2. Calendar → Shared calendars
3. Publish a calendar → pick yours → **Can view all details** → Publish
4. Copy the `.ics` URL (not the HTML one)

### 5. GitHub repo secrets
Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `GMAIL_USER` | Your Gmail address e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | The 16-char app password (no spaces) |
| `OWNER_EMAIL` | Same Gmail — where the digest is delivered |
| `FROM_EMAIL` | `morning@arfaz.ca` |
| `RESEND_API_KEY` | Your Resend API key |
| `ICS_URL` | The Outlook `.ics` URL |
| `FORWARD_EMAILS` | Comma-separated list of extra addresses to BCC e.g. `you@outlook.com,other@gmail.com` — leave empty to skip |

### 6. Gmail filter (keeps inbox clean)
1. Gmail → Settings → See all settings → Filters and Blocked Addresses → Create new filter
2. **To:** `morning@arfaz.ca`
3. Click **Create filter** → check **Skip the Inbox (Archive it)**
4. Create filter

Emails sent to `morning@arfaz.ca` land in All Mail (not your inbox). The script finds them there.

### 7. cron-job.org (reliable hourly trigger)
GitHub's built-in cron is unreliable (runs 1–3 hours late). cron-job.org fires exactly on the hour.

1. Sign up at cron-job.org
2. Create a new cron job — **Common tab:**
   - URL: `https://api.github.com/repos/YOUR_USERNAME/DailyDigest/actions/workflows/daily-email.yml/dispatches`
   - Schedule: every hour at `:00`
3. **Advanced tab:**
   - Request method: `POST`
   - Request body: `{"ref":"main"}`
   - Add headers:

| Key | Value |
|---|---|
| `Authorization` | `Bearer YOUR_GITHUB_PAT` |
| `Content-Type` | `application/json` |
| `Accept` | `application/vnd.github.v3+json` |

**Getting the GitHub PAT:**
- GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
- Repository: your DailyDigest repo
- Permissions → Actions: **Read and write**
- Generate and copy the token

Hit **Test run** — you should see `204 No Content`. Check your GitHub Actions tab for a new run.

## Editing tasks

Send any email to `morning@arfaz.ca` from any address. Within the hour you'll get a reply showing what changed.

**Commands (one per line, send as many as you want):**

```
add: pick up groceries
done: buy milk
clear
```

**Replace your whole list** — send a bullet list:
```
- call dentist
- submit report
- pick up dry cleaning
```

**Accepted from any sender** — ping@arfaz.ca, your Outlook, your other Gmail, anything. As long as it's addressed to `morning@arfaz.ca` it gets processed.

If a command isn't understood, you'll get a reply explaining the valid formats.

## What you receive

**6 AM every morning:** a styled digest with today's upcoming calendar events (past events filtered out) and your full task list.

**Any time you edit tasks:** an immediate confirmation showing what was added, removed, or not found — plus your updated list.

**Digest also BCC'd** to any addresses in `FORWARD_EMAILS` so you can read it across all your inboxes.

## File structure

```
├── scripts/digest.py        main script
├── templates/email.html     digest email template (Jinja2 + inline CSS)
├── tasks.md                 your task list (edit directly or via email)
├── requirements.txt         Python dependencies
└── .github/workflows/
    └── daily-email.yml      GitHub Actions workflow
```

## Customising the digest email

Edit `templates/email.html`. Variables available in the template:

| Variable | Example |
|---|---|
| `weekday` | `Monday` |
| `date_long` | `May 11, 2026` |
| `events` | list of `{time, title}` dicts |
| `tasks` | list of strings |

## Cost

| Service | Cost |
|---|---|
| GitHub Actions | Free (~360 min/month, well under the 2,000 limit) |
| cron-job.org | Free |
| Resend | Free (3,000 emails/month, you use ~60) |
| ImprovMX | Free |
| **Total** | **Free** |

## Troubleshooting

**No digest at 6 AM** — check the Actions tab. Common causes: wrong Gmail app password, ICS URL expired, Resend API key wrong.

**Task reply not picked up** — make sure the email was sent TO `morning@arfaz.ca`. Check the Actions run log for the IMAP search results.

**Cron job returning 404** — the GitHub PAT token expired or lost permissions. Regenerate it.

**Cron job returning 401** — the `Authorization` header value must start with `Bearer ` (with a space) before the token.

**Calendar showing old events** — Outlook's published ICS feed refreshes every few hours. Events added recently may not appear until the next feed refresh.
