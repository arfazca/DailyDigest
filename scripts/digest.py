#!/usr/bin/env python3
"""Daily digest: reads inbox commands, updates tasks.md, sends styled email with calendar + tasks."""

import email
import imaplib
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from email_reply_parser import EmailReplyParser
from icalendar import Calendar
from jinja2 import Template
from premailer import transform


# ---------- Config ----------
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
OWNER_EMAIL = os.environ["OWNER_EMAIL"]
FROM_EMAIL = os.environ["FROM_EMAIL"]           # e.g. morning@arfaz.ca
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
ICS_URL = os.environ["ICS_URL"]
TIMEZONE = os.environ.get("TIMEZONE", "America/Vancouver")

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO_ROOT / "tasks.md"
TEMPLATE_FILE = REPO_ROOT / "templates" / "email.html"

TZ = ZoneInfo(TIMEZONE)
NOW = datetime.now(TZ)
TODAY = NOW.date()


# ---------- Tasks file ----------
def read_tasks():
    if not TASKS_FILE.exists():
        return []
    tasks = []
    for line in TASKS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^[-*+]\s+(.*)$", line)
        tasks.append(m.group(1).strip() if m else line)
    return tasks


def write_tasks(tasks):
    body = "# Tasks\n\n" + ("\n".join(f"- {t}" for t in tasks) + "\n" if tasks else "")
    TASKS_FILE.write_text(body)


# ---------- Command parsing ----------
def parse_command(body: str):
    """Return (action, payload). Actions: add, remove, clear, replace, none."""
    clean = EmailReplyParser.parse_reply(body).strip()
    if not clean:
        return ("none", None)
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    bullets = [re.match(r"^[-*+]\s+(.*)$", l) for l in lines]
    if len(lines) >= 2 and all(bullets):
        return ("replace", [b.group(1).strip() for b in bullets])
    first = lines[0]
    if first.lower() == "clear":
        return ("clear", None)
    m = re.match(r"^(add|remove|done|del|delete)\s*:\s*(.+)$", first, re.IGNORECASE)
    if m:
        action = m.group(1).lower()
        if action in ("done", "del", "delete"):
            action = "remove"
        return (action, m.group(2).strip())
    return ("none", None)


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


# ---------- IMAP polling ----------
def process_inbox():
    """Apply pending reply-commands. Returns (final_tasks, change_summaries)."""
    print(f"-> IMAP login as {GMAIL_USER}")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    M.select("INBOX")
    typ, data = M.search(None, f'(UNSEEN FROM "{OWNER_EMAIL}" SUBJECT "Daily digest")')
    msg_ids = data[0].split()
    print(f"  {len(msg_ids)} unread message(s) from {OWNER_EMAIL}")

    tasks = read_tasks()
    summaries = []

    for msg_id in msg_ids:
        typ, msg_data = M.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        body = get_text_body(msg)
        action, payload = parse_command(body)
        print(f"  msg {msg_id.decode()}: action={action}, payload={payload!r}")

        if action == "add":
            tasks.append(payload)
            summaries.append(f"Added: {payload}")
        elif action == "remove":
            target = payload.lower()
            before = len(tasks)
            tasks = [t for t in tasks if target not in t.lower()]
            if len(tasks) < before:
                summaries.append(f"Removed: {payload}")
        elif action == "clear":
            if tasks:
                summaries.append("Cleared all tasks")
            tasks = []
        elif action == "replace":
            count = len(payload)
            summaries.append(f"Replaced list ({count} item{'s' if count != 1 else ''})")
            tasks = payload

        M.store(msg_id, "+FLAGS", "\\Seen")

    M.close()
    M.logout()
    if summaries:
        write_tasks(tasks)
        print(f"  changes: {summaries}")
    return tasks, summaries


# ---------- Calendar ----------
def _expand_to_local(start):
    if hasattr(start, "tzinfo"):
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        return start.astimezone(TZ), False
    return datetime.combine(start, datetime.min.time(), tzinfo=TZ), True


def fetch_events():
    print("-> Fetching ICS feed")
    r = requests.get(ICS_URL, timeout=30)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)
    today_start = datetime.combine(TODAY, datetime.min.time(), tzinfo=TZ)
    today_end = today_start + timedelta(days=1)
    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("dtstart")
        if not dtstart:
            continue
        start_local, is_all_day = _expand_to_local(dtstart.dt)
        if not (today_start <= start_local < today_end):
            continue
        title = str(component.get("summary", "(no title)"))
        time_str = "All day" if is_all_day else start_local.strftime("%-I:%M %p").lower()
        events.append({"time": time_str, "title": title, "_sort": start_local})
    events.sort(key=lambda e: (e["time"] == "All day", e["_sort"]))
    print(f"  {len(events)} event(s) today")
    return events


# ---------- Resend helper ----------
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
    print(f"  sent (id: {r.json().get('id')})")


# ---------- Morning digest ----------
def send_digest(events, tasks):
    template = Template(TEMPLATE_FILE.read_text())
    rendered = template.render(
        weekday=NOW.strftime("%A"),
        date_long=NOW.strftime("%B %-d, %Y"),
        events=events,
        tasks=tasks,
    )
    html = transform(rendered)
    subject = f"Daily digest — {NOW.strftime('%a %b %-d')}"
    print(f"-> Sending digest: {FROM_EMAIL} -> {OWNER_EMAIL}")
    _resend(subject, html)


# ---------- Instant confirmation ----------
def send_confirmation(summaries: list, tasks: list):
    """Sent immediately when the morning run detects reply-commands."""
    change_rows = "".join(
        '<div style="padding:9px 0;font-size:15px;color:#2a2a2a;border-bottom:1px solid #f3efe7;">'
        + s + "</div>"
        for s in summaries
    )
    if tasks:
        task_rows = "".join(
            '<div style="padding:9px 0;font-size:15px;color:#2a2a2a;border-bottom:1px solid #f3efe7;">'
            '<span style="color:#b85c2b;font-weight:700;margin-right:10px;">&#9675;</span>'
            + t + "</div>"
            for t in tasks
        )
    else:
        task_rows = (
            '<p style="color:#b5b0a5;font-style:italic;font-size:14px;margin:0;">'
            "Your list is empty.</p>"
        )

    html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0"/>'
        "</head>"
        '<body style="margin:0;padding:24px 12px;background:#f5f1ea;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;\">"
        '<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;'
        'overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04);">'

        '<div style="background:#2d6a4f;padding:22px 32px;">'
        '<p style="margin:0;font-size:12px;color:#95d5b2;text-transform:uppercase;'
        'letter-spacing:1.8px;font-weight:700;">Tasks updated</p>'
        '<p style="margin:6px 0 0;font-size:22px;font-weight:700;color:#fff;">Got it &#10003;</p>'
        "</div>"

        '<div style="padding:22px 32px;border-bottom:1px solid #ece7df;">'
        '<p style="font-size:11px;color:#8a8579;text-transform:uppercase;letter-spacing:1.8px;'
        'font-weight:700;margin:0 0 12px;">What changed</p>'
        + change_rows +
        "</div>"

        '<div style="padding:22px 32px;">'
        '<p style="font-size:11px;color:#8a8579;text-transform:uppercase;letter-spacing:1.8px;'
        'font-weight:700;margin:0 0 12px;">Your list now</p>'
        + task_rows +
        "</div>"

        '<div style="padding:16px 32px 20px;background:#faf8f3;font-size:12px;'
        'color:#8a8579;line-height:1.6;">'
        "You'll get the full digest with your calendar tomorrow morning."
        "</div>"

        "</div></body></html>"
    )

    n = len(summaries)
    subject = f"Tasks updated \u2014 {n} change{'s' if n != 1 else ''}"
    print(f"-> Sending confirmation to {OWNER_EMAIL}")
    _resend(subject, html)


# ---------- Git ----------
def git_commit_if_changed():
    result = subprocess.run(
        ["git", "status", "--porcelain", "tasks.md"], capture_output=True, text=True
    )
    if not result.stdout.strip():
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
    print("-> Committed tasks.md")


def main():
    tasks, summaries = process_inbox()

    # If there were changes: fire confirmation immediately + commit
    if summaries:
        send_confirmation(summaries, tasks)
        git_commit_if_changed()

    # Always send the morning digest
    events = fetch_events()
    send_digest(events, tasks)


if __name__ == "__main__":
    main()
