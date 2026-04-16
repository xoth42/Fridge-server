#!/usr/bin/env python3
"""Check alerts.wanglab@gmail.com inbox for a specific alert text.

Uses IMAP (default: imap.gmail.com) with app password auth.
Searches recent messages and returns success when query is found in subject/body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import imaplib
import os
import re
import sys
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime


def decode_mime_header(raw_value: str | None) -> str:
    if not raw_value:
        return ""
    parts = decode_header(raw_value)
    out: list[str] = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            out.append(payload.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(payload)
    return "".join(out)


def extract_text_body(msg: Message) -> str:
    if msg.is_multipart():
        texts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                texts.append(payload.decode(charset, errors="replace"))
        return "\n".join(texts)

    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check sender inbox for an alert")
    parser.add_argument("--imap-host", default=os.getenv("SENDER_IMAP_HOST", "imap.gmail.com"))
    parser.add_argument("--imap-port", type=int, default=int(os.getenv("SENDER_IMAP_PORT", "993")))
    parser.add_argument("--email", default=os.getenv("SENDER_EMAIL", "alerts.wanglab@gmail.com"))
    parser.add_argument(
        "--app-password",
        default=os.getenv("SENDER_APP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD"),
        help="IMAP app password for sender inbox",
    )
    parser.add_argument("--mailbox", default=os.getenv("SENDER_MAILBOX", "INBOX"))
    parser.add_argument("--query", default="Dodo test 9")
    parser.add_argument("--since-minutes", type=int, default=180)
    parser.add_argument("--max-messages", type=int, default=200)
    args = parser.parse_args()

    if not args.app_password:
        print("Missing app password. Set --app-password or SENDER_APP_PASSWORD/GMAIL_APP_PASSWORD.")
        return 2

    now_utc = dt.datetime.now(dt.timezone.utc)
    lower_bound = now_utc - dt.timedelta(minutes=args.since_minutes)
    needle = args.query.lower()

    print(f"Connecting to IMAP {args.imap_host}:{args.imap_port} as {args.email}")
    mail = imaplib.IMAP4_SSL(args.imap_host, args.imap_port)
    mail.login(args.email, args.app_password)
    try:
        status, _ = mail.select(args.mailbox, readonly=True)
        if status != "OK":
            print(f"Failed to open mailbox: {args.mailbox}")
            return 1

        status, data = mail.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            print("No messages found.")
            return 1

        ids = data[0].split()
        ids = ids[-args.max_messages :]

        for msg_id in reversed(ids):
            status, fetched = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not fetched or fetched[0] is None:
                continue

            raw_bytes = fetched[0][1]
            if not isinstance(raw_bytes, (bytes, bytearray)):
                continue

            msg = email.message_from_bytes(raw_bytes)

            date_hdr = msg.get("Date")
            msg_dt = None
            if date_hdr:
                try:
                    msg_dt = parsedate_to_datetime(date_hdr)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=dt.timezone.utc)
                    else:
                        msg_dt = msg_dt.astimezone(dt.timezone.utc)
                except Exception:
                    msg_dt = None

            if msg_dt and msg_dt < lower_bound:
                continue

            subject = decode_mime_header(msg.get("Subject"))
            from_hdr = decode_mime_header(msg.get("From"))
            to_hdr = decode_mime_header(msg.get("To"))
            body = extract_text_body(msg)

            haystack = f"{subject}\n{body}".lower()
            if needle in haystack:
                snippet = normalize_whitespace(body)[:260]
                print("MATCH_FOUND")
                print(f"Subject: {subject}")
                print(f"From: {from_hdr}")
                print(f"To: {to_hdr}")
                print(f"Date: {date_hdr or ''}")
                print(f"Snippet: {snippet}")
                return 0

        print("NO_MATCH")
        print(
            f"No message matching query '{args.query}' found in last {args.since_minutes} minutes "
            f"(checked up to {args.max_messages} latest messages)."
        )
        return 1
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()


if __name__ == "__main__":
    raise SystemExit(main())
