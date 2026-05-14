#!/usr/bin/env python3

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import fetchers
import imap_inbox
import parser as cmd_parser
import render
import sender


SCHEDULED_HOURS = {6, 12, 18}


def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def _apply_command(conn, cmd, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    a = cmd.action
    p = cmd.payload

    if a == "add_short":
        if db.short_task_exists(conn, p["text"]):
            change_log.append(f"already on short list: {p['text']}")
            return
        db.add_short_task(conn, p["text"], p.get("bucket"), p.get("due_at"))
        suffix = f" #{p['bucket']}" if p.get("bucket") else ""
        when = f" (due {p['due_at'].strftime('%a %b %-d %-I:%M %p').lower()})" if p.get("due_at") else ""
        change_log.append(f"added: {p['text']}{suffix}{when}")
        db.add_pending_change(conn, "add_short", {"text": p["text"]})
        return

    if a == "add_long":
        db.add_long_task(conn, p["text"], p["due_date"])
        change_log.append(f"added long task: {p['text']} (due {p['due_date'].strftime('%a %b %-d, %Y')})")
        db.add_pending_change(conn, "add_long", {"text": p["text"], "due": p["due_date"].isoformat()})
        return

    if a == "add_countdown":
        db.add_countdown(conn, p["name"], p["target_datetime"])
        change_log.append(f"countdown set: {p['name']} → {p['target_datetime'].strftime('%a %b %-d, %Y %-I:%M %p').lower()}")
        db.add_pending_change(conn, "add_countdown", {"name": p["name"]})
        return

    if a == "add_reflection":
        expires = cmd_parser.compute_reflection_expiry(p["period"], now)
        db.add_reflection(conn, p["text"], p["period"], expires)
        change_log.append(f"reflection set ({p['period']}): {p['text']} — clears {expires.strftime('%a %b %-d, %Y')} 11:59 PM")
        db.add_pending_change(conn, "add_reflection", {"text": p["text"]})
        return

    if a == "add_calendar":
        db.add_calendar(conn, p["name"], p["url"])
        change_log.append(f"calendar added: {p['name']}")
        db.add_pending_change(conn, "add_calendar", {"name": p["name"]})
        return

    if a == "done_short":
        row = db.remove_short_task(conn, p["match"])
        if row:
            change_log.append(f"removed: {row['text']}")
            db.add_pending_change(conn, "done_short", {"text": row["text"]})
        else:
            not_found.append(p["match"])
        return

    if a == "done_long":
        row = db.remove_long_task(conn, p["match"])
        if row:
            change_log.append(f"removed long task: {row['text']}")
            db.add_pending_change(conn, "done_long", {"text": row["text"]})
        else:
            not_found.append(p["match"])
        return

    if a == "done_countdown":
        row = db.remove_countdown(conn, p["match"])
        if row:
            change_log.append(f"removed countdown: {row['name']}")
        else:
            not_found.append(p["match"])
        return

    if a == "done_reflection":
        row = db.remove_reflection(conn, p["match"])
        if row:
            change_log.append(f"removed reflection: {row['text']}")
        else:
            not_found.append(p["match"])
        return

    if a == "done_calendar":
        n = db.remove_calendar_by_name(conn, p["name"])
        if n:
            change_log.append(f"removed calendar: {p['name']}")
        else:
            not_found.append(p["name"])
        return

    if a == "show_countdown":
        return


def _collect_inbox(conn, tz: ZoneInfo, now: datetime):
    msgs = imap_inbox.read_unprocessed(conn)

    commands_all = []
    show_full = False
    show_partials: set[str] = set()
    unknowns: list[tuple[str, str, str]] = []
    countdown_requested_name: str | None = None
    has_keyword_lines = False

    for m in msgs:
        pr = cmd_parser.parse_email(m["body"], tz)
        if pr.has_keyword_lines:
            has_keyword_lines = True
        if pr.show_full:
            show_full = True
        show_partials |= pr.show_partials
        commands_all.extend([(m, c) for c in pr.commands])
        for line, reason in pr.unknowns:
            unknowns.append((m["subject"], line, reason))
        for c in pr.commands:
            if c.action == "show_countdown" and c.payload.get("name"):
                countdown_requested_name = c.payload["name"]

    change_log: list[str] = []
    not_found: list[str] = []
    for m, c in commands_all:
        try:
            _apply_command(conn, c, now, change_log, not_found)
        except Exception as exc:
            db.log(conn, "ERROR", f"command {c.action} failed: {exc}")
            unknowns.append((m["subject"], c.raw_line, f"internal error: {exc}"))

    for m in msgs:
        db.mark_email_processed(
            conn,
            m["gmail_id"],
            m["subject"],
            sum(1 for mm, _ in commands_all if mm["gmail_id"] == m["gmail_id"]),
            sum(1 for s, _, _ in unknowns if s == m["subject"]),
        )

    return {
        "show_full": show_full,
        "show_partials": show_partials,
        "change_log": change_log,
        "not_found": not_found,
        "unknowns": unknowns,
        "countdown_requested_name": countdown_requested_name,
        "has_keyword_lines": has_keyword_lines,
        "n_messages": len(msgs),
    }


def _build_context(conn, tz: ZoneInfo, now: datetime, sections: set[str], inbox: dict) -> dict:
    today = now.date()
    profile = db.get_profile(conn)

    ctx: dict = {
        "weekday": now.strftime("%A"),
        "date_long": now.strftime("%B %-d, %Y"),
        "now_label": now.strftime("%-I:%M %p").lower(),
        "today_iso": today.isoformat(),
        "sections": sections,
        "change_log": inbox["change_log"],
        "not_found": inbox["not_found"],
        "unknowns": inbox["unknowns"],
    }

    if "age" in sections and profile.get("birthdate"):
        ctx["age"] = render.age_block(profile["birthdate"], today)

    if "weather" in sections or "timetable" in sections:
        if profile.get("weather_lat") and profile.get("weather_lon"):
            w = fetchers.fetch_weather(conn, float(profile["weather_lat"]), float(profile["weather_lon"]), tz, now)
            ctx["weather"] = render.shape_weather(w)

    if "calendar" in sections or "timetable" in sections or "due" in sections:
        events = fetchers.fetch_all_calendars(conn, tz, now)
        ctx["events"] = events
        ctx["timeline"] = render.shape_timeline(events, now)

    if "short" in sections or "grocery" in sections:
        ctx["short_tasks"] = render.shape_short_tasks(db.short_tasks(conn))

    if "long" in sections:
        ctx["long_tasks"] = render.shape_long_tasks(db.long_tasks(conn), today)

    if "due" in sections:
        events_for_due = ctx.get("events") or []
        ctx["dues"] = render.build_dues(db.long_tasks(conn), db.short_tasks(conn), events_for_due, today)

    if "countdown" in sections or "countdowns" in sections:
        single = inbox.get("countdown_requested_name") if "countdown" in sections and "countdowns" not in sections else None
        ctx["countdowns"] = render.shape_countdowns(db.countdowns(conn), now, single)

    if "reflection" in sections:
        ctx["reflections"] = render.shape_reflections(db.reflections(conn))

    if "quote" in sections:
        ctx["quote"] = fetchers.fetch_quote(conn, now)

    return ctx


def _full_sections(now: datetime) -> set[str]:
    return {"age", "calendar", "weather", "short", "long", "due", "countdowns", "reflection", "quote"}


def _decide_email(now: datetime, inbox: dict) -> tuple[str | None, set[str]]:
    scheduled = now.hour in SCHEDULED_HOURS
    has_changes = bool(inbox["change_log"] or inbox["not_found"])
    has_unknowns = bool(inbox["unknowns"])
    show_full = inbox["show_full"]
    partials = inbox["show_partials"]

    if show_full or scheduled:
        s = _full_sections(now)
        return ("digest", s)

    if partials:
        s: set[str] = set()
        for p in partials:
            if p == "timetable":
                s.update({"calendar", "weather"})
            elif p == "countdowns":
                s.add("countdowns")
            else:
                s.add(p)
        if has_changes:
            s.add("changes_banner")
        return ("partial", s)

    if has_changes:
        return ("update", {"calendar", "weather", "changes_banner"})

    if has_unknowns or inbox["has_keyword_lines"]:
        return ("errors_only", set())

    return (None, set())


def _subject_for(kind: str, now: datetime, inbox: dict) -> str:
    when = now.strftime("%a %b %-d %-I:%M %p").lower()
    if kind == "digest":
        return f"Daily digest — {now.strftime('%a %b %-d')}"
    if kind == "partial":
        labels = sorted(inbox["show_partials"])
        return f"Show: {', '.join(labels)} — {when}"
    if kind == "update":
        n = len(inbox["change_log"])
        return f"Tasks updated — {n} change{'s' if n != 1 else ''}"
    if kind == "errors_only":
        return f"Could not parse {len(inbox['unknowns'])} line(s)"
    return f"DailyDigest — {when}"


def main() -> None:
    tz = ZoneInfo(os.environ.get("TIMEZONE") or "America/Vancouver")
    now = _now(tz)
    conn = db.connect()

    db.log(conn, "INFO", f"=== run start {db.RUN_ID} at {now.isoformat()} ===")

    try:
        db.run_migrations(conn)
        db.seed_from_env(conn)
        db.prune_debug_log(conn, days=7)
        db.prune_old_pending(conn, days=14)
    except Exception as exc:
        db.log(conn, "ERROR", f"migrations/seed failed: {exc}")
        raise

    try:
        inbox = _collect_inbox(conn, tz, now)
    except Exception as exc:
        db.log(conn, "ERROR", f"inbox processing failed: {exc}")
        inbox = {
            "show_full": False,
            "show_partials": set(),
            "change_log": [],
            "not_found": [],
            "unknowns": [],
            "countdown_requested_name": None,
            "has_keyword_lines": False,
            "n_messages": 0,
        }

    kind, sections = _decide_email(now, inbox)
    db.log(conn, "INFO", f"decision: kind={kind} sections={sorted(sections)}")

    if kind is None:
        db.log(conn, "INFO", "nothing to send; exiting")
        conn.close()
        return

    ctx = _build_context(conn, tz, now, sections, inbox)
    html = render.render(sections, ctx)
    subject = _subject_for(kind, now, inbox)
    sender.send(conn, subject, html)

    if kind == "digest" or kind == "update" or (kind == "partial" and inbox["change_log"]):
        db.mark_pending_changes_notified(conn)

    db.log(conn, "INFO", f"=== run end {db.RUN_ID} ===")
    conn.close()


if __name__ == "__main__":
    main()
