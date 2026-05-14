from __future__ import annotations

import ipaddress
import os
import random
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

import db


DUE_EVENT_RX = "due"
SHORT_DURATION_SECONDS = 120


def _is_safe_ics_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False
        if hostname in ("localhost", "localhost.localdomain"):
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def _ical_dt_to_local(dt_val, tz: ZoneInfo) -> tuple[datetime, bool]:
    if hasattr(dt_val, "tzinfo"):
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=tz)
        return dt_val.astimezone(tz), False
    return datetime.combine(dt_val, datetime.min.time(), tzinfo=tz), True


def _ical_end_to_local(dt_prop, tz: ZoneInfo) -> datetime | None:
    if dt_prop is None:
        return None
    e = dt_prop.dt
    if hasattr(e, "tzinfo"):
        if e.tzinfo is None:
            e = e.replace(tzinfo=tz)
        return e.astimezone(tz)
    return datetime.combine(e, datetime.min.time(), tzinfo=tz)


def fetch_calendar_events(calendar_row: dict, start: datetime, end: datetime, tz: ZoneInfo, conn) -> list[dict]:
    cid = calendar_row["id"]
    name = calendar_row["name"]
    url = calendar_row["ics_url"]
    if not _is_safe_ics_url(url):
        db.log(conn, "WARN", f"calendar {name}: skipped non-https or private URL")
        return []
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
        start_local, is_all_day = _ical_dt_to_local(dtstart.dt, tz)
        end_local = _ical_end_to_local(component.get("dtend"), tz)
        title = str(component.get("summary", "(no title)"))
        uid = str(component.get("uid", ""))
        duration = (end_local - start_local).total_seconds() if end_local else None
        is_due = DUE_EVENT_RX in title.lower() or (duration is not None and duration <= SHORT_DURATION_SECONDS)
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


def _fetch_raw_weather(conn, lat: float, lon: float, api_key: str) -> dict | None:
    try:
        r = requests.get(
            "https://api.openweathermap.org/data/3.0/onecall",
            params={"lat": lat, "lon": lon, "exclude": "minutely,daily,alerts", "units": "metric", "appid": api_key},
            timeout=30,
        )
        if r.status_code == 401:
            db.log(conn, "WARN", f"OneCall 3.0 401: {r.text[:300]}")
            r = requests.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={"lat": lat, "lon": lon, "units": "metric", "appid": api_key},
                timeout=30,
            )
            if r.status_code == 401:
                db.log(conn, "WARN", f"2.5/forecast 401: {r.text[:300]}")
                probe = requests.get(
                    "https://api.openweathermap.org/data/2.5/weather",
                    params={"lat": lat, "lon": lon, "appid": api_key},
                    timeout=15,
                )
                db.log(conn, "WARN", f"2.5/weather probe status={probe.status_code} body={probe.text[:200]}")
                return None
        r.raise_for_status()
        payload = r.json()
        db.weather_cache_put(conn, payload)
        return payload
    except Exception as exc:
        db.log(conn, "ERROR", f"weather fetch failed: {exc}")
        return None


def _extract_hourly_entry(h: dict, is_forecast_list: bool) -> dict:
    weather0 = (h.get("weather") or [{}])[0]
    if is_forecast_list:
        main = h.get("main") or {}
        temp = round(main.get("temp", 0))
        feels_like = round(main.get("feels_like", 0))
    else:
        temp = round(h.get("temp", 0))
        feels_like = round(h.get("feels_like", 0))
    return {
        "temp": temp,
        "feels_like": feels_like,
        "summary": weather0.get("main", ""),
        "description": weather0.get("description", ""),
        "icon": weather0.get("icon", ""),
        "pop": round((h.get("pop") or 0) * 100),
    }


def _build_hourly(payload: dict, now: datetime, tz: ZoneInfo, midnight: datetime) -> list[dict]:
    cutoff = now.replace(minute=0, second=0, microsecond=0)
    is_forecast_list = "list" in payload
    entries = payload.get("hourly") or payload.get("list") or []
    out: list[dict] = []
    for h in entries:
        dt = datetime.fromtimestamp(h["dt"], tz=tz)
        if dt < cutoff or dt >= midnight:
            continue
        entry = _extract_hourly_entry(h, is_forecast_list)
        entry["dt"] = dt
        out.append(entry)
    return out


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
        db.log(conn, "INFO", f"weather: fetching (key len={len(api_key)}, lat={lat}, lon={lon})")
        payload = _fetch_raw_weather(conn, lat, lon, api_key)
        if payload is None:
            return None

    midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz) + timedelta(days=1)
    hourly_out = _build_hourly(payload, now, tz, midnight)

    summary = ""
    if hourly_out:
        temps = [h["temp"] for h in hourly_out]
        mid = sorted(temps)[len(temps) // 2]
        descs = [h["description"] for h in hourly_out if h["description"]]
        common = max(set(descs), key=descs.count) if descs else ""
        if common:
            summary = f"Mostly {mid}°C, {common}."
        else:
            summary = f"Mostly around {mid}°C."

    return {"hourly": hourly_out, "summary": summary}


POOL_TARGET = 12
ZEN_TAKE = 10
QUOTABLE_TAKE = 5

FALLBACK_QUOTE = {
    "text": "The struggle you're in today is developing the strength you need for tomorrow.",
    "author": "Robert Tew",
    "source": "fallback",
}


def _normalize_quote(text: str, author: str, source: str) -> dict | None:
    text = (text or "").strip()
    author = (author or "").strip()
    if not (10 <= len(text) <= 500):
        return None
    return {"text": text, "author": author, "source": source}


def _fetch_quotes_zen(conn, n: int) -> list[dict]:
    try:
        r = requests.get("https://zenquotes.io/api/quotes", timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        db.log(conn, "WARN", f"zenquotes pool failed: {exc}")
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        q = _normalize_quote(item.get("q", ""), item.get("a", ""), "zenquotes")
        if q:
            out.append(q)
    random.shuffle(out)
    return out[:n]


def _fetch_quotes_quotable(conn, n: int) -> list[dict]:
    try:
        r = requests.get(
            "https://api.quotable.io/quotes/random",
            params={
                "limit": min(n, 20),
                "tags": "perseverance|success|wisdom|courage|inspirational|motivational",
                "maxLength": 240,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        db.log(conn, "WARN", f"quotable pool failed: {exc}")
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        q = _normalize_quote(item.get("content", ""), item.get("author", ""), "quotable")
        if q:
            out.append(q)
    return out[:n]


def _build_quote_pool(conn, for_date) -> list[dict]:
    zen = _fetch_quotes_zen(conn, ZEN_TAKE)
    quotable = _fetch_quotes_quotable(conn, QUOTABLE_TAKE)
    combined = zen + quotable
    seen: set[str] = set()
    deduped: list[dict] = []
    for q in combined:
        key = q["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)
    if not deduped:
        return []
    inserted = db.quote_pool_put_many(conn, for_date, deduped)
    db.log(
        conn, "INFO",
        f"quote pool: fetched zen={len(zen)} quotable={len(quotable)} "
        f"deduped={len(deduped)} inserted={inserted}",
    )
    return db.quote_pool_for_date(conn, for_date)


def fetch_quote(conn, now: datetime) -> dict:
    today = now.date()
    db.quote_pool_prune(conn, days=7)

    pool = db.quote_pool_for_date(conn, today)
    if len(pool) < POOL_TARGET:
        fresh = _build_quote_pool(conn, today)
        if fresh:
            pool = fresh

    if not pool:
        recent = db.quote_pool_recent(conn, days=7)
        if recent:
            db.log(conn, "WARN", "quote pool: today empty; serving from recent days")
            pool = recent

    if not pool:
        db.log(conn, "WARN", "quote pool: empty; using hardcoded fallback")
        return FALLBACK_QUOTE

    chosen = random.choice(pool)
    db.log(conn, "INFO", f"quote: picked from pool of {len(pool)} ({chosen.get('source')})")
    return {
        "text": chosen["text"],
        "author": chosen.get("author") or "",
        "source": chosen.get("source") or "",
    }
