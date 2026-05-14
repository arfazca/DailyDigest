from __future__ import annotations

import os

import requests

import db


def send(conn, subject: str, html: str) -> str | None:
    payload = {
        "from": os.environ["FROM_EMAIL"],
        "to": [os.environ["OWNER_EMAIL"]],
        "reply_to": os.environ["FROM_EMAIL"],
        "subject": subject,
        "html": html,
    }
    extras = [e.strip() for e in os.environ.get("FORWARD_EMAILS", "").split(",") if e.strip()]
    if extras:
        payload["bcc"] = extras
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        rid = r.json().get("id")
        db.log(conn, "INFO", f"Resend sent id={rid} subject={subject!r}")
        return rid
    except Exception as exc:
        db.log(conn, "ERROR", f"Resend send failed: {exc}")
        return None
