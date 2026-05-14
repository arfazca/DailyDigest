from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import dateparser
from email_reply_parser import EmailReplyParser


VERBS = ("add", "+", "done", "remove", "delete", "del", "show")

PERIOD_TO_DELTA: dict[str, timedelta] = {
    "half-week": timedelta(days=3, hours=12),
    "week": timedelta(days=7),
    "half-month": timedelta(days=15),
    "month": timedelta(days=30),
    "half-year": timedelta(days=182),
    "year": timedelta(days=365),
    "6-month": timedelta(days=182),
    "6 month": timedelta(days=182),
    "six-month": timedelta(days=182),
    "six month": timedelta(days=182),
}

SHOW_FILLERS = {"me", "the", "a", "all", "my", "us"}

SHOW_PARTIAL_ALIASES: dict[str, str] = {
    "calendar": "calendar",
    "calendars": "calendar",
    "calender": "calendar",
    "calenders": "calendar",
    "schedule": "calendar",
    "weather": "weather",
    "forecast": "weather",
    "timetable": "timetable",
    "timetables": "timetable",
    "short": "short",
    "short list": "short",
    "list": "short",
    "current": "short",
    "current list": "short",
    "tasks": "short",
    "task": "short",
    "long": "long",
    "long list": "long",
    "long tasks": "long",
    "long task list": "long",
    "due": "due",
    "dues": "due",
    "countdown": "countdown",
    "countdowns": "countdowns",
    "reflection": "reflection",
    "reflections": "reflection",
    "quote": "quote",
    "quotes": "quote",
    "age": "age",
    "grocery": "grocery",
    "grocery bucket": "grocery",
    "grocery list": "grocery",
}

SHOW_FULL = {"everything", "all", "current", "now", ""}

GROCERY_LONG_ERR = (
    "show grocery from long list is not supported — "
    "the long task list does not use buckets."
)


@dataclass
class Command:
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw_line: str = ""


@dataclass
class ParseResult:
    commands: list[Command] = field(default_factory=list)
    show_full: bool = False
    show_partials: set[str] = field(default_factory=set)
    unknowns: list[tuple[str, str]] = field(default_factory=list)
    has_keyword_lines: bool = False


def _smart_unquote(s: str) -> str:
    return (
        s.replace("“", '"')
         .replace("”", '"')
         .replace("‘", "'")
         .replace("’", "'")
    )


def _extract_quoted(s: str) -> str | None:
    s = _smart_unquote(s)
    m = re.search(r'"([^"]+)"', s)
    if m:
        return m.group(1).strip()
    m = re.search(r"'([^']+)'", s)
    if m:
        return m.group(1).strip()
    return None


def _extract_all_quoted(s: str) -> list[str]:
    s = _smart_unquote(s)
    return [m.group(1).strip() for m in re.finditer(r'"([^"]+)"|\'([^\']+)\'', s) if m.group(1) or m.group(2)]


def _normalize_first_word(line: str) -> tuple[str, str]:
    stripped = line.lstrip("> \t")
    if not stripped:
        return "", line
    first = stripped.split(None, 1)
    head = first[0].lower()
    rest = first[1] if len(first) > 1 else ""
    return head, rest


def _strip_show_fillers(words: list[str]) -> list[str]:
    out = list(words)
    while out and out[0].lower() in SHOW_FILLERS:
        out.pop(0)
    return out


_PERIOD_CANONICAL: dict[str, str] = {
    "half-week": "half-week",
    "week": "week",
    "half-month": "half-month",
    "month": "month",
    "half-year": "half-year",
    "year": "year",
    "6-month": "half-year",
    "6 month": "half-year",
    "six-month": "half-year",
    "six month": "half-year",
}


def _match_partial_alias(joined: str) -> str | None:
    for key in sorted(SHOW_PARTIAL_ALIASES.keys(), key=len, reverse=True):
        if joined == key or joined.startswith(key + " ") or joined.endswith(" " + key) or f" {key} " in f" {joined} ":
            return SHOW_PARTIAL_ALIASES[key]
    return None


def _classify_show(rest: str) -> tuple[str | None, str | None, str | None]:
    rest = _smart_unquote(rest).strip()
    if not rest:
        return ("full", None, None)

    quoted = _extract_quoted(rest)
    words = re.split(r"\s+", re.sub(r'"[^"]*"|\'[^\']*\'', "", rest)).copy()
    words = [w for w in words if w]
    words = _strip_show_fillers(words)
    lowered = [w.lower() for w in words]

    if not lowered or lowered[0] in SHOW_FULL:
        return ("full", None, None)

    if "grocery" in lowered:
        if any(w in lowered for w in ("long",)):
            return (None, None, GROCERY_LONG_ERR)
        return ("grocery", None, None)

    if lowered[0] in ("countdown",) and (quoted or len(lowered) > 1):
        name = quoted if quoted else " ".join(words[1:])
        return ("countdown", name, None)
    if lowered[0] == "countdowns":
        return ("countdowns", None, None)

    alias = _match_partial_alias(" ".join(lowered))
    if alias:
        return (alias, None, None)

    return (None, None, f"unrecognized show target: {rest!r}")


def _parse_datetime(s: str, tz: ZoneInfo) -> datetime | None:
    s = s.strip().strip(",")
    if not s:
        return None
    dt = dateparser.parse(
        s,
        settings={
            "DATE_ORDER": "DMY",
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": str(tz),
            "TO_TIMEZONE": str(tz),
        },
    )
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _split_on_due(payload_text: str) -> tuple[str, str | None]:
    parts = re.split(r"\bdue\b\s*[:\-]?\s*", payload_text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return payload_text.strip(), None


def _parse_add_long(rest: str, quoted: str, tz: ZoneInfo) -> Command | tuple[None, str]:
    after_q = rest.split('"' if '"' in rest else "'", 2)
    date_blob = after_q[2] if len(after_q) == 3 else ""
    _, due_part = _split_on_due(date_blob)
    if not due_part:
        tail_words = re.sub(r'"[^"]*"|\'[^\']*\'', "", rest)
        _, due_part = _split_on_due(tail_words)
    if not due_part:
        return None, "add long task: missing due date — e.g. due 15 august 2026"
    due = _parse_datetime(due_part, tz)
    if not due:
        return None, f"add long task: could not parse date {due_part!r}"
    return Command("add_long", {"text": quoted, "due_date": due.date()}, rest), None


def _parse_add_countdown(rest: str, quoted: str, tz: ZoneInfo) -> Command | tuple[None, str]:
    after_q = rest.split('"' if '"' in rest else "'", 2)
    tail = after_q[2].strip() if len(after_q) == 3 else ""
    tail = re.sub(r"^(at|on|for|to)\b", "", tail, flags=re.IGNORECASE).strip()
    target = _parse_datetime(tail, tz)
    if not target:
        return None, f"add countdown: could not parse target {tail!r}"
    return Command("add_countdown", {"name": quoted, "target_datetime": target}, rest), None


def _parse_add_reflection(rest: str, quoted: str) -> Command | tuple[None, str]:
    lowered = rest.lower()
    period = None
    for key in sorted(PERIOD_TO_DELTA.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", lowered):
            period = _PERIOD_CANONICAL.get(key, key)
            break
    if period is None:
        return None, "add reflection: missing period (half-week/week/half-month/month/half-year/year)"
    return Command("add_reflection", {"text": quoted, "period": period}, rest), None


def _parse_add_calendar(rest: str, quoted: str) -> Command | tuple[None, str]:
    m = re.search(r"https://\S+", rest)
    if not m:
        return None, "add calendar: missing https:// URL"
    return Command("add_calendar", {"name": quoted, "url": m.group(0)}, rest), None


def _parse_add_short(rest: str, quoted: str, tz: ZoneInfo) -> Command | tuple[None, str]:
    bucket = None
    m = re.search(r"#(\w+)", rest)
    if m:
        bucket = m.group(1).lower()
    after_q = rest.split('"' if '"' in rest else "'", 2)
    tail = after_q[2] if len(after_q) == 3 else ""
    _, due_part = _split_on_due(tail)
    due_at = None
    if due_part:
        due_at = _parse_datetime(due_part, tz)
        if not due_at:
            return None, f"add: could not parse due {due_part!r}"
    return Command("add_short", {"text": quoted, "bucket": bucket, "due_at": due_at}, rest), None


def _parse_add(rest: str, tz: ZoneInfo) -> Command | tuple[None, str]:
    quoted = _extract_quoted(rest)
    lowered = rest.lower()

    if re.search(r"\blong(\s+task)?\b", lowered):
        if quoted is None:
            return None, "add long task: missing quoted text"
        return _parse_add_long(rest, quoted, tz)

    if re.search(r"\bcountdown\b", lowered):
        if quoted is None:
            return None, "add countdown: missing quoted name"
        return _parse_add_countdown(rest, quoted, tz)

    if re.search(r"\breflection\b", lowered):
        if quoted is None:
            return None, "add reflection: missing quoted text"
        return _parse_add_reflection(rest, quoted)

    if re.search(r"\bcalendar\b", lowered):
        if quoted is None:
            return None, "add calendar: missing quoted name"
        return _parse_add_calendar(rest, quoted)

    if quoted is None:
        return None, 'add: missing quoted text (use add "your task")'

    return _parse_add_short(rest, quoted, tz)


_DONE_DISPATCH = [
    ("long ", "done_long", "match"),
    ("countdown", "done_countdown", "match"),
    ("reflection", "done_reflection", "match"),
    ("calendar", "done_calendar", "name"),
]


def _parse_done(rest: str) -> Command | tuple[None, str]:
    lowered = rest.lower().strip()
    quoted = _extract_quoted(rest)

    for prefix, action, pkey in _DONE_DISPATCH:
        if lowered.startswith(prefix):
            tail = rest[len(prefix):].strip().strip('"\'')
            target = quoted or tail
            if not target:
                return None, f"done {prefix.strip()}: missing target"
            return Command(action, {pkey: target}, rest), None

    target = quoted or rest.strip().strip('"\'')
    if not target:
        return None, "done: missing target"
    return Command("done_short", {"match": target}, rest), None


def _handle_show_line(rest: str, line: str, result: ParseResult) -> None:
    kind, arg, err = _classify_show(rest)
    if err:
        result.unknowns.append((line, err))
        return
    if kind == "full":
        result.show_full = True
    elif kind == "countdown":
        result.show_partials.add("countdown")
        result.commands.append(Command("show_countdown", {"name": arg or ""}, line))
    else:
        result.show_partials.add(kind)


def _handle_add_line(rest: str, line: str, tz: ZoneInfo, result: ParseResult) -> None:
    cmd, err = _parse_add(rest, tz)
    if cmd:
        result.commands.append(cmd)
    else:
        result.unknowns.append((line, err))


def _handle_done_line(rest: str, line: str, result: ParseResult) -> None:
    cmd, err = _parse_done(rest)
    if cmd:
        result.commands.append(cmd)
    else:
        result.unknowns.append((line, err))


def parse_email(body: str, tz: ZoneInfo) -> ParseResult:
    clean = EmailReplyParser.parse_reply(body or "").strip()
    result = ParseResult()
    if not clean:
        return result

    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        head, rest = _normalize_first_word(line)
        if head not in VERBS:
            continue
        result.has_keyword_lines = True

        if head == "show":
            _handle_show_line(rest, line, result)
        elif head in ("add", "+"):
            _handle_add_line(rest, line, tz, result)
        elif head in ("done", "remove", "delete", "del"):
            _handle_done_line(rest, line, result)

    return result


def compute_reflection_expiry(period: str, now: datetime) -> datetime:
    delta = PERIOD_TO_DELTA.get(period)
    if delta is None:
        delta = timedelta(days=7)
    end = (now + delta).date()
    return datetime.combine(end, time(23, 59, 0), tzinfo=now.tzinfo)
