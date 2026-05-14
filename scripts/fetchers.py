from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

import db


DUE_EVENT_RX = "due"
SHORT_DURATION_SECONDS = 120


def fetch_calendar_events(calendar_row: dict, start: datetime, end: datetime, tz: ZoneInfo, conn) -> list[dict]:
    cid = calendar_row["id"]
    name = calendar_row["name"]
    url = calendar_row["ics_url"]
    db.log(conn, "INFO", f"fetching ICS: {name} ({url[:80]})")
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except Exception as exc:
        db.log(conn, "ERROR", f"ICS fetch failed for {name}: {exc}")
        return []

    try:
        cal = Calendar.from_ical(r.content)
    except Exception as exc:
        db.log(conn, "ERROR", f"ICS parse failed for {name}: {exc}")
        return []

    try:
        raw_events = recurring_ical_events.of(cal).between(start, end)
    except Exception as exc:
        db.log(conn, "ERROR", f"ICS expand failed for {name}: {exc}")
        return []

    out: list[dict] = []
    for component in raw_events:
        dtstart = component.get("dtstart")
        if not dtstart:
            continue
        s = dtstart.dt
        if hasattr(s, "tzinfo"):
            if s.tzinfo is None:
                s = s.replace(tzinfo=tz)
            start_local = s.astimezone(tz)
            is_all_day = False
        else:
            start_local = datetime.combine(s, datetime.min.time(), tzinfo=tz)
            is_all_day = True

        dtend = component.get("dtend")
        end_local = None
        if dtend:
            e = dtend.dt
            if hasattr(e, "tzinfo"):
                if e.tzinfo is None:
                    e = e.replace(tzinfo=tz)
                end_local = e.astimezone(tz)
            else:
                end_local = datetime.combine(e, datetime.min.time(), tzinfo=tz)

        title = str(component.get("summary", "(no title)"))
        uid = str(component.get("uid", ""))

        duration = (end_local - start_local).total_seconds() if end_local else None
        is_due = (
            DUE_EVENT_RX in title.lower()
            or (duration is not None and duration <= SHORT_DURATION_SECONDS)
        )

        out.append({
            "calendar_id": cid,
            "calendar_name": name,
            "uid": uid,
            "summary": title,
            "dtstart": start_local,
            "dtend": end_local,
            "is_all_day": is_all_day,
            "is_due": is_due,
        })

    try:
        db.replace_events_cache(conn, cid, [
            {"uid": r["uid"], "summary": r["summary"], "dtstart": r["dtstart"],
             "dtend": r["dtend"], "is_all_day": r["is_all_day"]}
            for r in out
        ])
    except Exception as exc:
        db.log(conn, "WARN", f"events_cache write failed for {name}: {exc}")

    return out


def fetch_all_calendars(conn, tz: ZoneInfo, now: datetime) -> list[dict]:
    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=1)
    cals = db.list_calendars(conn, only_enabled=True)
    all_events: list[dict] = []
    for c in cals:
        all_events.extend(fetch_calendar_events(c, start, end, tz, conn))
    all_events.sort(key=lambda e: (not e["is_all_day"], e["dtstart"]))
    db.log(conn, "INFO", f"calendars: {len(cals)}, total events today: {len(all_events)}")
    return all_events


def fetch_weather(conn, lat: float, lon: float, tz: ZoneInfo, now: datetime) -> dict | None:
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        db.log(conn, "WARN", "OPENWEATHER_API_KEY not set; skipping weather")
        return None

    cached = db.weather_cache_get_fresh(conn, max_age_minutes=360)
    if cached:
        db.log(conn, "INFO", f"weather: cache hit (fetched {cached['fetched_at']})")
        payload = cached["payload"]
    else:
        try:
            r = requests.get(
                "https://api.openweathermap.org/data/3.0/onecall",
                params={
                    "lat": lat,
                    "lon": lon,
                    "exclude": "minutely,daily,alerts",
                    "units": "metric",
                    "appid": api_key,
                },
                timeout=30,
            )
            if r.status_code == 401:
                db.log(conn, "WARN", "OneCall 3.0 401; trying 2.5/forecast")
                r = requests.get(
                    "https://api.openweathermap.org/data/2.5/forecast",
                    params={"lat": lat, "lon": lon, "units": "metric", "appid": api_key},
                    timeout=30,
                )
            r.raise_for_status()
            payload = r.json()
            db.weather_cache_put(conn, payload)
        except Exception as exc:
            db.log(conn, "ERROR", f"weather fetch failed: {exc}")
            return None

    midnight = (datetime.combine(now.date(), datetime.min.time(), tzinfo=tz) + timedelta(days=1))

    hourly_out: list[dict] = []
    if "hourly" in payload:
        for h in payload["hourly"]:
            dt = datetime.fromtimestamp(h["dt"], tz=tz)
            if dt < now.replace(minute=0, second=0, microsecond=0):
                continue
            if dt >= midnight:
                continue
            weather0 = (h.get("weather") or [{}])[0]
            hourly_out.append({
                "dt": dt,
                "temp": round(h.get("temp", 0)),
                "feels_like": round(h.get("feels_like", 0)),
                "summary": weather0.get("main", ""),
                "description": weather0.get("description", ""),
                "icon": weather0.get("icon", ""),
                "pop": round((h.get("pop") or 0) * 100),
            })
    elif "list" in payload:
        for h in payload["list"]:
            dt = datetime.fromtimestamp(h["dt"], tz=tz)
            if dt < now.replace(minute=0, second=0, microsecond=0):
                continue
            if dt >= midnight:
                continue
            weather0 = (h.get("weather") or [{}])[0]
            main = h.get("main") or {}
            hourly_out.append({
                "dt": dt,
                "temp": round(main.get("temp", 0)),
                "feels_like": round(main.get("feels_like", 0)),
                "summary": weather0.get("main", ""),
                "description": weather0.get("description", ""),
                "icon": weather0.get("icon", ""),
                "pop": round((h.get("pop") or 0) * 100),
            })

    summary = ""
    if hourly_out:
        temps = [h["temp"] for h in hourly_out]
        mid = sorted(temps)[len(temps) // 2]
        descs = [h["description"] for h in hourly_out if h["description"]]
        common = max(set(descs), key=descs.count) if descs else ""
        summary = f"Mostly {mid}°C, {common}." if common else f"Mostly around {mid}°C."

    return {"hourly": hourly_out, "summary": summary}


def fetch_quote(conn, today) -> dict:
    cached = db.quote_cache_get_recent(conn, max_age_days=7)
    if cached:
        return cached

    try:
        r = requests.get("https://zenquotes.io/api/random", timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            text = data[0].get("q", "").strip()
            author = data[0].get("a", "").strip()
            if 10 <= len(text) <= 500:
                db.quote_cache_put(conn, today, text, author, "zenquotes")
                return {"text": text, "author": author, "source": "zenquotes"}
    except Exception as exc:
        db.log(conn, "WARN", f"zenquotes failed: {exc}")

    try:
        r = requests.get(
            "https://api.quotable.io/random",
            params={"tags": "perseverance|success|wisdom|courage"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("content") or "").strip()
        author = (data.get("author") or "").strip()
        if text:
            db.quote_cache_put(conn, today, text, author, "quotable")
            return {"text": text, "author": author, "source": "quotable"}
    except Exception as exc:
        db.log(conn, "WARN", f"quotable failed: {exc}")

    fallback = {
        "text": "The struggle you’re in today is developing the strength you need for tomorrow.",
        "author": "Robert Tew",
        "source": "fallback",
    }
    db.quote_cache_put(conn, today, fallback["text"], fallback["author"], fallback["source"])
    return fallback
