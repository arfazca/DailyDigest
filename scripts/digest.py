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


def parse_commands(body: str):
    clean = EmailReplyParser.parse_reply(body).strip()
    log(f"  Cleaned body: {clean[:200]!r}")
    if not clean:
        return []

    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    commands = []

    for line in lines:
        if line.lower() == "clear":
            commands.append(("clear", None))
            continue
        m = re.match(r"^(add|remove|done|del|delete)\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            action = m.group(1).lower()
            if action in ("done", "del", "delete"):
                action = "remove"
            commands.append((action, m.group(2).strip()))

    if commands:
        return commands

    bullets = [re.match(r"^[-*+]\s+(.*)$", l) for l in lines]
    if len(lines) >= 2 and all(bullets):
        return [("replace", [b.group(1).strip() for b in bullets])]

    return [("unknown", clean[:300])]


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
    search = f'(SINCE "{since}" FROM "{SENDER_EMAIL}" SUBJECT "Re: Daily digest")'
    log(f"IMAP search: {search}")

    typ, data = M.search(None, search)
    msg_ids = data[0].split()
    log(f"Found {len(msg_ids)} reply(ies)")

    tasks      = read_tasks()
    successes  = []
    not_found  = []
    unknowns   = []

    for msg_id in msg_ids:
        typ, msg_data = M.fetch(msg_id, "(RFC822 FLAGS)")
        flags = imaplib.ParseFlags(msg_data[0][0])
        log(f"  Message {msg_id.decode()} flags: {flags}")

        if b"\\Seen" in flags:
            log("  Skipping — already processed")
            continue

        msg      = email.message_from_bytes(msg_data[0][1])
        subject  = msg.get("Subject", "")
        log(f"  Subject: {subject!r}")
        body     = get_text_body(msg)
        commands = parse_commands(body)
        log(f"  Commands: {commands}")

        for action, payload in commands:
            if action == "add":
                tasks.append(payload)
                successes.append(f"Added: {payload}")
            elif action == "remove":
                target = payload.lower()
                before = len(tasks)
                tasks  = [t for t in tasks if target not in t.lower()]
                if len(tasks) < before:
                    successes.append(f"Removed: {payload}")
                else:
                    log(f"  Not found: '{payload}'")
                    not_found.append(payload)
            elif action == "clear":
                tasks = []
                successes.append("Cleared all tasks")
            elif action == "replace":
                tasks = payload
                successes.append(f"Replaced list ({len(payload)} item{'s' if len(payload) != 1 else ''})")
            elif action == "unknown":
                log(f"  Unrecognised: {payload!r}")
                unknowns.append(payload)

        M.store(msg_id, "+FLAGS", "\\Seen")

    M.close()
    M.logout()

    if successes:
        write_tasks(tasks)
        log(f"Changes: {successes}")

    return tasks, successes, not_found, unknowns


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

    raw_events = recurring_ical_events.of(cal).between(today_start, today_end)
    log(f"Found {len(raw_events)} raw event(s)")

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

        if not is_all_day and start_local < NOW:
            log(f"  Skipping past event: {component.get('summary')} at {start_local}")
            continue

        title    = str(component.get("summary", "(no title)"))
        time_str = "All day" if is_all_day else start_local.strftime("%-I:%M %p").lower()
        log(f"  {time_str} — {title}")
        events.append({"time": time_str, "title": title, "_sort": start_local})

    events.sort(key=lambda e: (e["time"] == "All day", e["_sort"]))
    log(f"{len(events)} upcoming event(s) after filtering past ones")
    return events


def _resend(subject: str, html: str):
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": FROM_EMAIL,
            "to": [OWNER_EMAIL],
            "reply_to": FROM_EMAIL,
            "subject": subject,
            "html": html,
            **( {"bcc": FORWARD_EMAILS} if FORWARD_EMAILS else {} ),
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
    subject = f"Daily digest — {NOW.strftime('%a %b %-d')}"
    log(f"Sending digest → {OWNER_EMAIL}")
    _resend(subject, html)


def _row(text, color="#2a2a2a"):
    return (
        f'<div style="padding:9px 0;font-size:15px;color:{color};'
        f'border-bottom:1px solid #f3efe7;">{text}</div>'
    )


def send_reply_summary(successes, not_found, unknowns, tasks):
    success_html = "".join(_row(s) for s in successes) if successes else ""
    not_found_html = "".join(
        _row(f'&#10007; &nbsp;<span style="color:#888;">{t}</span> — not in list', "#2a2a2a")
        for t in not_found
    ) if not_found else ""
    unknown_html = "".join(
        f'<div style="padding:8px 12px;background:#f5f1ea;border-radius:6px;'
        f'font-family:monospace;font-size:13px;color:#5a5550;margin-bottom:8px;">{u}</div>'
        for u in unknowns
    ) if unknowns else ""

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

    changes_section = ""
    if successes or not_found:
        changes_section = (
            "<div style='padding:22px 32px;border-bottom:1px solid #ece7df;'>"
            "<p style='font-size:11px;color:#8a8579;text-transform:uppercase;letter-spacing:1.8px;"
            "font-weight:700;margin:0 0 12px;'>What changed</p>"
            + success_html + not_found_html +
            "</div>"
        )

    unknown_section = ""
    if unknowns:
        unknown_section = (
            "<div style='padding:22px 32px;border-bottom:1px solid #ece7df;background:#fffaf7;'>"
            "<p style='font-size:11px;color:#8a8579;text-transform:uppercase;letter-spacing:1.8px;"
            "font-weight:700;margin:0 0 12px;'>Couldn't parse</p>"
            "<p style='font-size:14px;color:#5a5550;margin:0 0 12px;'>"
            "These lines weren't recognised as commands:</p>"
            + unknown_html +
            "<p style='font-size:12px;color:#8a8579;margin:12px 0 0;'>"
            "Valid: <code style='background:#ece7df;padding:2px 6px;border-radius:4px;'>add: X</code> &nbsp;"
            "<code style='background:#ece7df;padding:2px 6px;border-radius:4px;'>done: X</code> &nbsp;"
            "<code style='background:#ece7df;padding:2px 6px;border-radius:4px;'>clear</code>"
            "</p></div>"
        )

    header_text = "Got it &#10003;" if successes else "Noted"
    html = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'/></head>"
        "<body style='margin:0;padding:24px 12px;background:#f5f1ea;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;'>"
        "<div style='max-width:560px;margin:0 auto;background:#fff;border-radius:12px;"
        "overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04);'>"
        "<div style='background:#2d6a4f;padding:22px 32px;'>"
        "<p style='margin:0;font-size:12px;color:#95d5b2;text-transform:uppercase;"
        "letter-spacing:1.8px;font-weight:700;'>Tasks updated</p>"
        f"<p style='margin:6px 0 0;font-size:22px;font-weight:700;color:#fff;'>{header_text}</p>"
        "</div>"
        + changes_section + unknown_section +
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

    n       = len(successes)
    subject = f"Tasks updated \u2014 {n} change{'s' if n != 1 else ''}" if successes else "Task reply received"
    log(f"Sending reply summary → {OWNER_EMAIL}")
    _resend(subject, html)


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

    tasks, successes, not_found, unknowns = process_inbox()

    if successes or not_found or unknowns:
        send_reply_summary(successes, not_found, unknowns, tasks)

    if successes:
        git_commit_if_changed()

    if NOW.hour == 6:
        log("6 AM — sending morning digest")
        events = fetch_events()
        send_digest(events, tasks)
    else:
        log(f"Hour {NOW.hour} — skipping digest (not 6 AM)")

    log("=== Done ===")


if __name__ == "__main__":
    main()
