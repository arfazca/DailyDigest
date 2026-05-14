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
        "today_pretty": today.strftime("%B %-d, %Y"),
        "next_age": years + 1,
        "to_next_months": max(0, n_months),
        "to_next_days": max(0, n_days),
        "to_next_total_days": delta.days,
    }


_SOURCE_LABELS: dict[str, str] = {
    "short": "Short task",
    "long": "Long task",
    "calendar": "Calendar",
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
            "source_label": _SOURCE_LABELS["long"],
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
            "source_label": _SOURCE_LABELS["short"],
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
            "source_label": _SOURCE_LABELS["calendar"],
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


def _humanize_completed(completed_at: datetime, now: datetime) -> str:
    delta = now - completed_at
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    days_diff = (now.date() - completed_at.date()).days
    if days_diff <= 0:
        return f"today, {completed_at.strftime('%-I:%M %p').lower()}"
    if days_diff == 1:
        return f"yesterday, {completed_at.strftime('%-I:%M %p').lower()}"
    return f"{days_diff} days ago"


def shape_recent_completed(short_rows: list[dict], long_rows: list[dict], now: datetime) -> list[dict]:
    out: list[dict] = []
    for r in short_rows:
        if not r.get("completed_at"):
            continue
        out.append({
            "text": r["text"],
            "source": "short",
            "completed_at": r["completed_at"],
            "when": _humanize_completed(r["completed_at"], now),
        })
    for r in long_rows:
        if not r.get("completed_at"):
            continue
        out.append({
            "text": r["text"],
            "source": "long",
            "completed_at": r["completed_at"],
            "when": _humanize_completed(r["completed_at"], now),
        })
    out.sort(key=lambda r: r["completed_at"], reverse=True)
    return out


def _c2_format_distance(meters: int | None) -> str:
    if not meters:
        return "—"
    if meters >= 10000:
        return f"{meters / 1000:.1f} km"
    if meters >= 1000:
        return f"{meters / 1000:.2f} km"
    return f"{meters} m"


def _c2_format_time(time_tenths: int | None) -> str:
    if not time_tenths:
        return "—"
    secs = time_tenths // 10
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _c2_format_pace(distance: int | None, time_tenths: int | None) -> str:
    if not distance or not time_tenths or distance < 100:
        return ""
    pace_tenths = round(time_tenths * 500 / distance)
    secs = pace_tenths // 10
    m, s = divmod(secs, 60)
    tenth = pace_tenths % 10
    return f"{m}:{s:02d}.{tenth}/500m"


def _c2_parse_dt(value, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.fromisoformat(str(value)).replace(tzinfo=tz) if value else datetime.now(tz)


def _c2_shape_item(r: dict, tz: ZoneInfo) -> dict:
    dt = _c2_parse_dt(r.get("date"), tz)
    type_lower = (r.get("type") or "").lower()
    rate_label = "rpm" if type_lower == "bike" else "spm"
    return {
        "date": dt,
        "date_label": dt.strftime("%a %b %-d"),
        "distance": _c2_format_distance(r.get("distance")),
        "time": _c2_format_time(r.get("time_tenths")) or r.get("time_formatted") or "—",
        "pace": _c2_format_pace(r.get("distance"), r.get("time_tenths")),
        "stroke_rate": r.get("stroke_rate"),
        "rate_label": rate_label,
        "heart_rate_avg": r.get("heart_rate_avg"),
        "calories": r.get("calories_total"),
        "comments": (r.get("comments") or "").strip() or None,
    }


def _c2_week_grid(week_raw: list[dict], week_start: date, today: date,
                   tz: ZoneInfo) -> list[dict]:
    grid = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        day_rows = [
            r for r in week_raw
            if _c2_parse_dt(r.get("date"), tz).date() == d
        ]
        total_m = sum((r.get("distance") or 0) for r in day_rows)
        grid.append({
            "day": d.strftime("%a"),
            "count": len(day_rows),
            "distance": _c2_format_distance(total_m) if total_m else None,
            "is_today": d == today,
            "is_future": d > today,
        })
    return grid


def shape_concept2(data: dict | None, now: datetime) -> dict | None:
    if not data:
        return None
    tz = now.tzinfo or ZoneInfo("UTC")
    today = now.date()
    week_start = data.get("week_start") or (today - timedelta(days=today.weekday()))

    week_raw = data.get("week") or []
    week_grid = _c2_week_grid(week_raw, week_start, today, tz)
    week_m = sum((r.get("distance") or 0) for r in week_raw)
    week_t = sum((r.get("time_tenths") or 0) for r in week_raw)
    week_count = sum(d["count"] for d in week_grid)

    recent_raw = data.get("recent") or []
    workouts = sorted(
        (_c2_shape_item(r, tz) for r in recent_raw),
        key=lambda x: x["date"],
        reverse=True,
    )
    month_m = sum((r.get("distance") or 0) for r in recent_raw)
    month_t = sum((r.get("time_tenths") or 0) for r in recent_raw)
    lifetime = data.get("lifetime") or {}

    return {
        "week_grid": week_grid,
        "week_count": week_count,
        "week_distance": _c2_format_distance(week_m),
        "week_time": _c2_format_time(week_t),
        "window_days": data.get("window_days", 30),
        "window_count": len(workouts),
        "window_distance": _c2_format_distance(month_m),
        "window_time": _c2_format_time(month_t),
        "lifetime_count": int(lifetime.get("n") or 0),
        "lifetime_distance": _c2_format_distance(int(lifetime.get("distance") or 0)),
        "lifetime_time": _c2_format_time(int(lifetime.get("time_tenths") or 0)),
        "workouts": workouts,
    }


def render(sections: set[str], context: dict) -> str:
    tpl = Template(TEMPLATE_FILE.read_text())
    ctx = dict(context)
    ctx["sections"] = sections
    rendered = tpl.render(**ctx)
    return transform(rendered)
