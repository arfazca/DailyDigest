from __future__ import annotations

import email
import imaplib
import os
import re

import db


def _decode(part) -> str:
    return part.get_payload(decode=True).decode(
        part.get_content_charset() or "utf-8", errors="replace"
    )


def _get_text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return _decode(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                html = _decode(part)
                return re.sub(r"<[^>]+>", "", html)
        return ""
    return _decode(msg)


def read_unprocessed(conn) -> list[dict]:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pw = os.environ["GMAIL_APP_PASSWORD"]
    from_email = os.environ["FROM_EMAIL"]

    db.log(conn, "INFO", f"IMAP connect {gmail_user}")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(gmail_user, gmail_pw)
    status, _ = M.select('"[Gmail]/All Mail"', readonly=False)
    db.log(conn, "INFO", f"select All Mail: {status}")

    query = f'"deliveredto:{from_email} newer_than:2d"'
    typ, data = M.search(None, "X-GM-RAW", query)
    if typ != "OK":
        db.log(conn, "WARN", f"IMAP search returned {typ}")
        M.close()
        M.logout()
        return []

    ids = data[0].split()
    db.log(conn, "INFO", f"IMAP returned {len(ids)} message(s)")

    results: list[dict] = []
    for mid in ids:
        typ, msg_data = M.fetch(mid, "(RFC822 FLAGS)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        flags = imaplib.ParseFlags(msg_data[0][0])
        msg = email.message_from_bytes(msg_data[0][1])
        gmail_id = msg.get("Message-ID") or f"uid-{mid.decode()}"

        if db.email_already_processed(conn, gmail_id):
            db.log(conn, "DEBUG", f"skip already-processed: {gmail_id}")
            if b"\\Seen" not in flags:
                M.store(mid, "+FLAGS", "\\Seen")
            continue

        if b"\\Seen" in flags:
            db.log(conn, "DEBUG", f"skip already \\Seen and not in DB: {gmail_id}")
            db.mark_email_processed(conn, gmail_id, msg.get("Subject", ""), 0, 0)
            continue

        body = _get_text_body(msg)
        results.append({
            "gmail_id": gmail_id,
            "subject": msg.get("Subject", "") or "",
            "from": msg.get("From", "") or "",
            "body": body,
            "imap_mid": mid,
        })
        M.store(mid, "+FLAGS", "\\Seen")

    M.close()
    M.logout()
    db.log(conn, "INFO", f"IMAP unprocessed: {len(results)}")
    return results
