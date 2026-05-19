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


def _handle_add_short(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    if db.short_task_exists(conn, p["text"]):
        change_log.append(f"already on short list: {p['text']}")
        return
    db.add_short_task(conn, p["text"], p.get("bucket"), p.get("due_at"))
    suffix = f" #{p['bucket']}" if p.get("bucket") else ""
    when = f" (due {render.format_email_datetime(p['due_at'])})" if p.get("due_at") else ""
    change_log.append(f"added: {p['text']}{suffix}{when}")
    db.add_pending_change(conn, "add_short", {"text": p["text"]})


def _handle_add_long(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    db.add_long_task(conn, p["text"], p["due_date"])
    change_log.append(f"added long task: {p['text']} (due {render.format_email_date(p['due_date'], include_year=True)})")
    db.add_pending_change(conn, "add_long", {"text": p["text"], "due": p["due_date"].isoformat()})


def _handle_add_countdown(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    db.add_countdown(conn, p["name"], p["target_datetime"])
    change_log.append(f"countdown set: {p['name']} \u2192 {render.format_email_datetime(p['target_datetime'], include_year=True)}")
    db.add_pending_change(conn, "add_countdown", {"name": p["name"]})


def _handle_add_reflection(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    expires = cmd_parser.compute_reflection_expiry(p["period"], now)
    db.add_reflection(conn, p["text"], p["period"], expires)
    change_log.append(f"reflection set ({p['period']}): {p['text']} \u2014 clears {render.format_email_date(expires, include_year=True)} 11:59 PM")
    db.add_pending_change(conn, "add_reflection", {"text": p["text"]})


def _handle_add_calendar(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    db.add_calendar(conn, p["name"], p["url"])
    change_log.append(f"calendar added: {p['name']}")
    db.add_pending_change(conn, "add_calendar", {"name": p["name"]})


def _handle_done_short(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    row = db.remove_short_task(conn, p["match"])
    if row:
        change_log.append(f"removed: {row['text']}")
        db.add_pending_change(conn, "done_short", {"text": row["text"]})
    else:
        not_found.append(p["match"])


def _handle_done_long(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    row = db.remove_long_task(conn, p["match"])
    if row:
        change_log.append(f"removed long task: {row['text']}")
        db.add_pending_change(conn, "done_long", {"text": row["text"]})
    else:
        not_found.append(p["match"])


def _handle_done_countdown(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    row = db.remove_countdown(conn, p["match"])
    if row:
        change_log.append(f"removed countdown: {row['name']}")
    else:
        not_found.append(p["match"])


def _handle_done_reflection(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    row = db.remove_reflection(conn, p["match"])
    if row:
        change_log.append(f"removed reflection: {row['text']}")
    else:
        not_found.append(p["match"])


def _handle_done_calendar(conn, p: dict, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    n = db.remove_calendar_by_name(conn, p["name"])
    if n:
        change_log.append(f"removed calendar: {p['name']}")
    else:
        not_found.append(p["name"])


_ACTION_HANDLERS: dict = {
    "add_short": _handle_add_short,
    "add_long": _handle_add_long,
    "add_countdown": _handle_add_countdown,
    "add_reflection": _handle_add_reflection,
    "add_calendar": _handle_add_calendar,
    "done_short": _handle_done_short,
    "done_long": _handle_done_long,
    "done_countdown": _handle_done_countdown,
    "done_reflection": _handle_done_reflection,
    "done_calendar": _handle_done_calendar,
}


def _apply_command(conn, cmd, now: datetime, change_log: list[str], not_found: list[str]) -> None:
    handler = _ACTION_HANDLERS.get(cmd.action)
    if handler:
        handler(conn, cmd.payload, now, change_log, not_found)


def _find_countdown_name(commands_all: list) -> str | None:
    name = None
    for _, c in commands_all:
        if c.action == "show_countdown" and c.payload.get("name"):
            name = c.payload["name"]
    return name


def _collect_inbox(conn, tz: ZoneInfo, now: datetime):
    msgs = imap_inbox.read_unprocessed(conn)

    commands_all = []
    show_full = False
    show_partials: set[str] = set()
    unknowns: list[tuple[str, str, str]] = []
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
        "countdown_requested_name": _find_countdown_name(commands_all),
        "has_keyword_lines": has_keyword_lines,
        "n_messages": len(msgs),
    }


def _fetch_weather_ctx(conn, profile: dict, sections: set[str], tz: ZoneInfo, now: datetime) -> dict | None:
    if "weather" not in sections and "timetable" not in sections:
        return None
    if not profile.get("weather_lat") or not profile.get("weather_lon"):
        return None
    w = fetchers.fetch_weather(conn, float(profile["weather_lat"]), float(profile["weather_lon"]), tz, now)
    return render.shape_weather(w)


def _fetch_calendar_ctx(conn, sections: set[str], tz: ZoneInfo, now: datetime) -> list[dict] | None:
    if "calendar" not in sections and "timetable" not in sections and "due" not in sections:
        return None
    return fetchers.fetch_all_calendars(conn, tz, now)


def _fetch_countdown_ctx(conn, sections: set[str], inbox: dict, now: datetime) -> list | None:
    if "countdown" not in sections and "countdowns" not in sections:
        return None
    single = inbox.get("countdown_requested_name") if "countdown" in sections and "countdowns" not in sections else None
    return render.shape_countdowns(db.countdowns(conn), now, single)


def _fetch_concept2_ctx(conn, sections: set[str], now: datetime) -> dict | None:
    if "concept2" not in sections:
        return None
    return render.shape_concept2(fetchers.fetch_concept2_data(conn, now), now)


def _build_context(conn, tz: ZoneInfo, now: datetime, sections: set[str], inbox: dict) -> dict:
    today = now.date()
    profile = db.get_profile(conn)

    ctx: dict = {
        "weekday": now.strftime("%A"),
        "date_long": render.format_email_date(now, include_weekday=False, include_year=True),
        "now_label": now.strftime("%-I:%M %p"),
        "today_iso": today.isoformat(),
        "sections": sections,
        "change_log": inbox["change_log"],
        "not_found": inbox["not_found"],
        "unknowns": inbox["unknowns"],
    }

    if "age" in sections and profile.get("birthdate"):
        ctx["age"] = render.age_block(profile["birthdate"], today)

    weather = _fetch_weather_ctx(conn, profile, sections, tz, now)
    if weather is not None:
        ctx["weather"] = weather

    events = _fetch_calendar_ctx(conn, sections, tz, now)
    if events is not None:
        ctx["events"] = events
        ctx["timeline"] = render.shape_timeline(events, now)

    if "short" in sections or "grocery" in sections:
        ctx["short_tasks"] = render.shape_short_tasks(db.short_tasks(conn))

    if "long" in sections:
        ctx["long_tasks"] = render.shape_long_tasks(db.long_tasks(conn), today)

    if "due" in sections:
        events_for_due = ctx.get("events") or []
        ctx["dues"] = render.build_dues(db.long_tasks(conn), db.short_tasks(conn), events_for_due, today)

    countdowns = _fetch_countdown_ctx(conn, sections, inbox, now)
    if countdowns is not None:
        ctx["countdowns"] = countdowns

    if "reflection" in sections:
        ctx["reflections"] = render.shape_reflections(db.reflections(conn))

    if "completed" in sections:
        ctx["recent_completed"] = render.shape_recent_completed(
            db.recent_completed_short(conn, days=3),
            db.recent_completed_long(conn, days=3),
            now,
        )

    if "quote" in sections:
        ctx["quote"] = fetchers.fetch_quote(conn, now)

    c2 = _fetch_concept2_ctx(conn, sections, now)
    if c2 is not None:
        ctx["concept2"] = c2

    return ctx


def _full_sections() -> set[str]:
    return {
        "age", "calendar", "weather", "short", "long", "due",
        "countdowns", "reflection", "completed", "concept2", "quote",
    }


def _sections_for_partials(partials: set[str]) -> set[str]:
    sections: set[str] = set()
    for p in partials:
        if p == "timetable":
            sections.update({"calendar", "weather"})
        elif p == "countdowns":
            sections.add("countdowns")
        else:
            sections.add(p)
    return sections


def _decide_email(now: datetime, inbox: dict) -> tuple[str | None, set[str]]:
    scheduled = now.hour in SCHEDULED_HOURS
    has_changes = bool(inbox["change_log"] or inbox["not_found"])
    has_unknowns = bool(inbox["unknowns"])
    show_full = inbox["show_full"]
    partials = inbox["show_partials"]

    if show_full or scheduled:
        return ("digest", _full_sections())

    if partials:
        s = _sections_for_partials(partials)
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
        db.prune_completed_tasks(conn, days=30)
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
