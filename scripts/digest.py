#!/usr/bin/env python3

import email
import imaplib
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from email_reply_parser import EmailReplyParser
from icalendar import Calendar
from jinja2 import Template
from premailer import transform

GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
OWNER_EMAIL        = os.environ["OWNER_EMAIL"]
FROM_EMAIL         = os.environ["FROM_EMAIL"]
SENDER_EMAIL       = os.environ["SENDER_EMAIL"]
RESEND_API_KEY     = os.environ["RESEND_API_KEY"]
ICS_URL            = os.environ["ICS_URL"]
TIMEZONE           = os.environ.get("TIMEZONE", "America/Vancouver")

REPO_ROOT     = Path(__file__).resolve().parent.parent
TASKS_FILE    = REPO_ROOT / "tasks.md"
TEMPLATE_FILE = REPO_ROOT / "templates" / "email.html"

TZ    = ZoneInfo(TIMEZONE)
NOW   = datetime.now(TZ)
TODAY = NOW.date()


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def read_tasks():
    if not TASKS_FILE.exists():
        log("tasks.md not found — starting empty")
        return []
    tasks = []
    for line in TASKS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^[-*+]\s+(.*)$", line)
        tasks.append(m.group(1).strip() if m else line)
    log(f"Read {len(tasks)} task(s): {tasks}")
    return tasks


def write_tasks(tasks):
    body = "# Tasks\n\n" + ("\n".join(f"- {t}" for t in tasks) + "\n" if tasks else "")
    TASKS_FILE.write_text(body)
    log(f"Wrote {len(tasks)} task(s)")


def parse_command(body: str):
    clean = EmailReplyParser.parse_reply(body).strip()
    log(f"  Cleaned body: {clean[:120]!r}")
    if not clean:
        return ("none", None)
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    first = lines[0]
    m = re.match(r"^(add|remove|done|del|delete)\s*:\s*(.+)$", first, re.IGNORECASE)
    if m:
        action = m.group(1).lower()
        if action in ("done", "del", "delete"):
            action = "remove"
        return (action, m.group(2).strip())
    return ("unknown", clean[:200])


def get_text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                html = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
                return re.sub(r"<[^>]+>", "", html)
        return ""
    return msg.get_payload(decode=True).decode(
        msg.get_content_charset() or "utf-8", errors="replace"
    )


def process_inbox():
    log(f"Connecting to IMAP as {GMAIL_USER}")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    status, _ = M.select("INBOX", readonly=False)
    log(f"INBOX select: {status}")

    since  = (NOW - timedelta(hours=36)).strftime("%d-%b-%Y")
    search = f'(SINCE "{since}" FROM "{SENDER_EMAIL}")'
    log(f"IMAP search: {search}")

    typ, data = M.search(None, search)
    msg_ids = data[0].split()
    log(f"Found {len(msg_ids)} reply(ies)")

    tasks           = read_tasks()
    summaries       = []
    unknown_attempts = []

    for msg_id in msg_ids:
        typ, msg_data = M.fetch(msg_id, "(RFC822 FLAGS)")
        flags = imaplib.ParseFlags(msg_data[0][0])
        log(f"  Message {msg_id.decode()} flags: {flags}")

        if b"\\Seen" in flags:
            log(f"  Skipping — already processed")
            continue

        msg     = email.message_from_bytes(msg_data[0][1])
        subject = msg.get("Subject", "")
        log(f"  Subject: {subject!r}")
        body             = get_text_body(msg)
        action, payload  = parse_command(body)
        log(f"  Parsed → action={action!r}, payload={payload!r}")

        if action == "add":
            tasks.append(payload)
            summaries.append(f"Added: {payload}")
        elif action == "remove":
            target = payload.lower()
            before = len(tasks)
            tasks  = [t for t in tasks if target not in t.lower()]
            if len(tasks) < before:
                summaries.append(f"Removed: {payload}")
            else:
                log(f"  No task matched '{payload}'")
                unknown_attempts.append(f'Tried to remove "{payload}" but nothing matched')
        elif action == "unknown":
            log(f"  Unrecognised: {payload!r}")
            unknown_attempts.append(payload)

        M.store(msg_id, "+FLAGS", "\\Seen")

    M.close()
    M.logout()

    if summaries:
        write_tasks(tasks)
        log(f"Changes: {summaries}")

    return tasks, summaries, unknown_attempts


def fetch_events():
    log("Fetching ICS feed")
    try:
        r = requests.get(ICS_URL, timeout=30)
        log(f"ICS: HTTP {r.status_code}, {len(r.content)} bytes")
        r.raise_for_status()
    except Exception as e:
        log(f"ERROR fetching ICS: {e}")
        return []

    cal         = Calendar.from_ical(r.content)
    today_start = datetime.combine(TODAY, datetime.min.time(), tzinfo=TZ)
    today_end   = today_start + timedelta(days=1)
    log(f"Events between {today_start} and {today_end}")

    raw_events = recurring_ical_events.of(cal).between(today_start, today_end)
    log(f"Found {len(raw_events)} event(s)")

    events = []
    for component in raw_events:
        dtstart = component.get("dtstart")
        if not dtstart:
            continue
        start = dtstart.dt
        if hasattr(start, "tzinfo"):
            if start.tzinfo is None:
                start = start.replace(tzinfo=TZ)
            start_local = start.astimezone(TZ)
            is_all_day  = False
        else:
            start_local = datetime.combine(start, datetime.min.time(), tzinfo=TZ)
            is_all_day  = True
        title    = str(component.get("summary", "(no title)"))
        time_str = "All day" if is_all_day else start_local.strftime("%-I:%M %p").lower()
        log(f"  {time_str} — {title}")
        events.append({"time": time_str, "title": title, "_sort": start_local})

    events.sort(key=lambda e: (e["time"] == "All day", e["_sort"]))
    return events


def _resend(subject: str, html: str):
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": FROM_EMAIL,
            "to": [OWNER_EMAIL],
            "reply_to": OWNER_EMAIL,
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )
    r.raise_for_status()
    log(f"Sent (id: {r.json().get('id')})")


def send_digest(events, tasks):
    template = Template(TEMPLATE_FILE.read_text())
    rendered = template.render(
        weekday=NOW.strftime("%A"),
        date_long=NOW.strftime("%B %-d, %Y"),
        events=events,
        tasks=tasks,
    )
    html    = transform(rendered)
    subject = f"Daily digest — {NOW.strftime('%a %b %-d, %-I:%M %p')}"
    log(f"Sending digest → {OWNER_EMAIL}")
    _resend(subject, html)


def send_confirmation(summaries: list, tasks: list):
    change_rows = "".join(
        '<div style="padding:9px 0;font-size:15px;color:#2a2a2a;border-bottom:1px solid #f3efe7;">'
        + s + "</div>"
        for s in summaries
    )
    task_rows = (
        "".join(
            '<div style="padding:9px 0;font-size:15px;color:#2a2a2a;border-bottom:1px solid #f3efe7;">'
            '<span style="color:#b85c2b;font-weight:700;margin-right:10px;">&#9675;</span>'
            + t + "</div>"
            for t in tasks
        )
        if tasks else
        '<p style="color:#b5b0a5;font-style:italic;font-size:14px;margin:0;">Your list is empty.</p>'
    )
    html = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'/></head>"
        "<body style='margin:0;padding:24px 12px;background:#f5f1ea;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;'>"
        "<div style='max-width:560px;margin:0 auto;background:#fff;border-radius:12px;"
        "overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04);'>"
        "<div style='background:#2d6a4f;padding:22px 32px;'>"
        "<p style='margin:0;font-size:12px;color:#95d5b2;text-transform:uppercase;"
        "letter-spacing:1.8px;font-weight:700;'>Tasks updated</p>"
        "<p style='margin:6px 0 0;font-size:22px;font-weight:700;color:#fff;'>Got it &#10003;</p>"
        "</div>"
        "<div style='padding:22px 32px;border-bottom:1px solid #ece7df;'>"
        "<p style='font-size:11px;color:#8a8579;text-transform:uppercase;letter-spacing:1.8px;"
        "font-weight:700;margin:0 0 12px;'>What changed</p>"
        + change_rows +
        "</div>"
        "<div style='padding:22px 32px;'>"
        "<p style='font-size:11px;color:#8a8579;text-transform:uppercase;letter-spacing:1.8px;"
        "font-weight:700;margin:0 0 12px;'>Your list now</p>"
        + task_rows +
        "</div>"
        "<div style='padding:16px 32px 20px;background:#faf8f3;font-size:12px;"
        "color:#8a8579;line-height:1.6;'>"
        "You'll get the full digest with your calendar tomorrow morning."
        "</div></div></body></html>"
    )
    n       = len(summaries)
    subject = f"Tasks updated \u2014 {n} change{'s' if n != 1 else ''}"
    log(f"Sending confirmation → {OWNER_EMAIL}")
    _resend(subject, html)


def send_unknown_reply(attempts: list):
    attempts_html = "".join(
        f'<div style="padding:8px 12px;background:#f5f1ea;border-radius:6px;'
        f'font-family:monospace;font-size:13px;color:#5a5550;margin-bottom:8px;">{a}</div>'
        for a in attempts
    )
    html = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'/></head>"
        "<body style='margin:0;padding:24px 12px;background:#f5f1ea;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;'>"
        "<div style='max-width:560px;margin:0 auto;background:#fff;border-radius:12px;"
        "overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04);'>"
        "<div style='background:#7c4d2a;padding:22px 32px;'>"
        "<p style='margin:0;font-size:12px;color:#f5c9a0;text-transform:uppercase;"
        "letter-spacing:1.8px;font-weight:700;'>Heads up</p>"
        "<p style='margin:6px 0 0;font-size:22px;font-weight:700;color:#fff;'>"
        "Didn't understand that</p>"
        "</div>"
        "<div style='padding:22px 32px;border-bottom:1px solid #ece7df;'>"
        "<p style='font-size:15px;color:#2a2a2a;margin:0 0 14px;'>"
        "I received your reply but couldn't parse the command:</p>"
        + attempts_html +
        "</div>"
        "<div style='padding:22px 32px;background:#faf8f3;font-size:13px;"
        "color:#5a5550;line-height:1.8;'>"
        "<strong style='color:#2a2a2a;'>Valid commands:</strong><br>"
        "<code style='background:#ece7df;padding:2px 7px;border-radius:4px;'>"
        "add: buy milk</code> &nbsp;add a task<br>"
        "<code style='background:#ece7df;padding:2px 7px;border-radius:4px;'>"
        "done: buy milk</code> &nbsp;remove a task"
        "</div></div></body></html>"
    )
    log(f"Sending unknown-command reply → {OWNER_EMAIL}")
    _resend("Didn't understand your task command", html)


def git_commit_if_changed():
    result = subprocess.run(
        ["git", "status", "--porcelain", "tasks.md"], capture_output=True, text=True
    )
    if not result.stdout.strip():
        log("No changes to commit")
        return
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(["git", "add", "tasks.md"], check=True)
    subprocess.run(
        ["git", "commit", "-m", f"Update tasks via email ({NOW.strftime('%Y-%m-%d')})"],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)
    log("Committed and pushed tasks.md")


def main():
    log("=== Daily Digest starting ===")
    log(f"Local time: {NOW.strftime('%Y-%m-%d %H:%M %Z')}")

    tasks, summaries, unknown_attempts = process_inbox()

    if summaries:
        send_confirmation(summaries, tasks)
        git_commit_if_changed()

    if unknown_attempts:
        send_unknown_reply(unknown_attempts)

    events = fetch_events()
    send_digest(events, tasks)

    log("=== Done ===")


if __name__ == "__main__":
    main()
