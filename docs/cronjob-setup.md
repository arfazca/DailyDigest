# cron-job.org Setup

GitHub Actions has an unreliable built-in cron scheduler that can run 1-3 hours late. cron-job.org fires on the hour by calling the GitHub API to trigger the workflow on demand. This doc covers the setup used for this repo.

## What it does

cron-job.org sends an HTTP POST to GitHub's workflow dispatch API every hour. GitHub receives it, queues the workflow run immediately, and the script runs within seconds.

## Prerequisites

- A GitHub account with the DailyDigest repo
- A GitHub fine-grained personal access token with Actions: Read and write permission on the repo
- A free cron-job.org account

## GitHub personal access token

1. Go to github.com -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens
2. Click Generate new token
3. Set a name like `DailyDigestToken`
4. Set expiration to No expiration (or however long you want)
5. Under Repository access, choose Only select repositories and pick your DailyDigest repo
6. Under Permissions -> Repository permissions, set Actions to Read and write
7. Click Generate token and copy it immediately

## cron-job.org configuration

### Common tab

| Field | Value |
|---|---|
| Title | Daily Digest |
| URL | `https://api.github.com/repos/YOUR_GITHUB_USERNAME/DailyDigest/actions/workflows/daily-email.yml/dispatches` |
| Schedule | Every hour at :00 |

Replace `YOUR_GITHUB_USERNAME` with your actual GitHub username.

### Advanced tab

**Request method:** POST

**Request body:**
```
{"ref":"main"}
```

**Headers** (click + ADD for each):

| Key | Value |
|---|---|
| `Authorization` | `Bearer github_pat_YOUR_TOKEN_HERE` |
| `Content-Type` | `application/json` |
| `Accept` | `application/vnd.github.v3+json` |

The `Bearer ` prefix with a space is required. The full value should be `Bearer github_pat_...`.

**Time zone:** America/Los_Angeles

**Timeout:** 30 seconds (default)

**Treat redirects with HTTP 3xx as success:** off (default)

## Testing

Click Test run. A successful response shows:

- Status: 204 No Content
- This is correct. GitHub returns no body on success.

After the test, check github.com/YOUR_USERNAME/DailyDigest/actions to confirm a new workflow run appeared.

## Troubleshooting

**401 Unauthorized** - The Authorization header value is wrong. Make sure it starts with `Bearer ` (with a space) before the token.

**404 Not Found** - The token doesn't have permission or the URL is wrong. Double-check the repo name in the URL matches exactly, and that the token has Actions: Read and write on that specific repo.

**No workflow run appeared after 204** - Check that the workflow file is named exactly `daily-email.yml` in `.github/workflows/` on the `main` branch.

## How the workflow decides what to send

The workflow runs every hour but the script checks the local time before sending:

- **Any hour:** checks Gmail for new emails sent to `morning@arfaz.ca` and processes task commands
- **6 AM, 12 PM, 6 PM:** sends the full digest (calendar + tasks)
- **Any hour with task changes:** also sends the full digest after the reply summary

So replying to the digest or emailing `morning@arfaz.ca` gets picked up within 60 minutes at most.
