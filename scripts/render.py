from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Template
from premailer import transform


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_FILE = REPO_ROOT / "templates" / "email.html"


def _color_for_days(days: int) -> str:
    if days < 0:
        return "overdue"
    if days <= 2:
        return "red"
    if days <= 3:
        return "red-orange"
    if days <= 5:
        return "orange"
    if days <= 7:
        return "yellow"
    return "green"


def _humanize_days(days: int) -> str:
    if days < 0:
        return f"{abs(days)} day{'s' if abs(days) != 1 else ''} overdue"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _ordinal_suffix(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _ordinal_day(day: int) -> str:
    return f"{day}<sup>{_ordinal_suffix(day)}</sup>"


def format_email_date(value: date | datetime, *, include_weekday: bool = True, include_year: bool = False) -> str:
    label = f"{value.strftime('%B')} {_ordinal_day(value.day)}"
    if include_year:
        label = f"{label}, {value.year}"
    if include_weekday:
        label = f"{value.strftime('%A')} {label}"
    return label


def format_email_datetime(value: datetime, *, include_weekday: bool = True, include_year: bool = False) -> str:
    return f"{format_email_date(value, include_weekday=include_weekday, include_year=include_year)} {value.strftime('%-I:%M %p')}"


def age_block(birthdate: date, today: date) -> dict:
    years = today.year - birthdate.year
    months = today.month - birthdate.month
    days = today.day - birthdate.day
    if days < 0:
        months -= 1
        last_month = (today.replace(day=1) - timedelta(days=1))
        days += last_month.day
    if months < 0:
        years -= 1
        months += 12

    next_bday_year = today.year if (today.month, today.day) < (birthdate.month, birthdate.day) else today.year + 1
    next_bday = birthdate.replace(year=next_bday_year)
    delta = next_bday - today
    n_months = (next_bday.year * 12 + next_bday.month) - (today.year * 12 + today.month)
    n_days = next_bday.day - today.day
    if n_days < 0:
        n_months -= 1
        last_month = (next_bday.replace(day=1) - timedelta(days=1))
        n_days += last_month.day

    return {
        "years": years,
        "months": months,
        "days": days,
        "today_iso": today.isoformat(),
        "next_age": years + 1,
        "to_next_months": max(0, n_months),
        "to_next_days": max(0, n_days),
        "to_next_total_days": delta.days,
    }


def build_dues(long_tasks: list[dict], short_tasks: list[dict], events: list[dict], today: date) -> list[dict]:
    out: list[dict] = []
    for lt in long_tasks:
        d = lt["due_date"]
        days = (d - today).days
        if days > 14:
            continue
        out.append({
            "label": lt["text"],
            "when": format_email_date(d),
            "days": days,
            "humanized": _humanize_days(days),
            "color": _color_for_days(days),
            "source": "long",
            "removable": True,
        })
    for st in short_tasks:
        du = st.get("due_at")
        if not du:
            continue
        days = (du.date() - today).days
        if days > 14:
            continue
        out.append({
            "label": st["text"],
            "when": format_email_datetime(du),
            "days": days,
            "humanized": _humanize_days(days),
            "color": _color_for_days(days),
            "source": "short",
            "removable": True,
        })
    for ev in events:
        if not ev.get("is_due"):
            continue
        d = ev["dtstart"].date()
        days = (d - today).days
        if days > 14:
            continue
        out.append({
            "label": ev["summary"],
            "when": format_email_datetime(ev["dtstart"]) if not ev.get("is_all_day") else format_email_date(ev["dtstart"]),
            "days": days,
            "humanized": _humanize_days(days),
            "color": _color_for_days(days),
            "source": "calendar",
            "removable": False,
        })
    out.sort(key=lambda d: (d["days"], d["label"]))
    return out


def shape_timeline(events: list[dict], now: datetime) -> list[dict]:
    out: list[dict] = []
    for ev in events:
        is_past = (not ev.get("is_all_day")) and ev["dtstart"] < now
        time_str = "All day" if ev.get("is_all_day") else ev["dtstart"].strftime("%-I:%M %p").lower()
        out.append({
            "time": time_str,
            "title": ev["summary"],
            "calendar_name": ev.get("calendar_name", ""),
            "past": is_past,
            "is_due": ev.get("is_due", False),
        })
    return out


def shape_weather(weather: dict | None) -> dict | None:
    if not weather:
        return None
    hourly = []
    for h in weather["hourly"]:
        hourly.append({
            "time": h["dt"].strftime("%-I %p").lower(),
            "temp": h["temp"],
            "summary": h["summary"],
            "description": h["description"],
            "precip_pct": h["pop"],
        })
    return {"summary": weather.get("summary", ""), "hourly": hourly}


def shape_short_tasks(rows: list[dict]) -> dict:
    main: list[dict] = []
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        item = {
            "text": r["text"],
            "due_at": format_email_datetime(r["due_at"]) if r.get("due_at") else None,
        }
        if r.get("bucket"):
            buckets.setdefault(r["bucket"], []).append(item)
        else:
            main.append(item)
    return {"main": main, "buckets": buckets}


def shape_long_tasks(rows: list[dict], today: date) -> list[dict]:
    out = []
    for r in rows:
        days = (r["due_date"] - today).days
        out.append({
            "text": r["text"],
            "due": format_email_date(r["due_date"], include_year=True),
            "days": days,
            "humanized": _humanize_days(days),
            "color": _color_for_days(days) if days <= 14 else "neutral",
        })
    return out


def _countdown_label(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total <= 0:
        return "REACHED"
    days = delta.days
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    if days == 0:
        return f"{hours}h {minutes}m"
    years, r = divmod(days, 365)
    months, r = divmod(r, 30)
    weeks, r = divmod(r, 7)
    if years:
        return f"{years}y {months}mo" if months else f"{years}y"
    if months:
        rem = weeks * 7 + r
        return f"{months}mo {rem}d" if rem else f"{months}mo"
    if weeks:
        return f"{weeks}w {r}d" if r else f"{weeks}w"
    return f"{days}d {hours}h"


def shape_countdowns(rows: list[dict], now: datetime, single: str | None = None) -> list[dict]:
    out = []
    for r in rows:
        if single and single.lower() not in r["name"].lower():
            continue
        delta = r["target_datetime"] - now
        label = _countdown_label(delta)
        days = max(0, delta.days)
        out.append({
            "name": r["name"],
            "target": format_email_datetime(r["target_datetime"], include_year=True),
            "label": label,
            "days": days,
        })
    out.sort(key=lambda c: c["days"])
    return out


def shape_reflections(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "text": r["text"],
            "period": r["period"],
            "expires_at": format_email_date(r["expires_at"], include_year=True),
        })
    return out


def render(sections: set[str], context: dict) -> str:
    tpl = Template(TEMPLATE_FILE.read_text())
    ctx = dict(context)
    ctx["sections"] = sections
    rendered = tpl.render(**ctx)
    return transform(rendered)
