"""Read what came back: replies, bounces, and opt-outs.

Closes the loop the send path leaves open. `sends` records that a message was
handed to a transport; nothing until now recorded that a human answered, that
it bounced, or that someone asked to be removed — which meant the unsubscribe
promise in the footer was still being kept by whoever happened to read the
mailbox that week.

Two mechanisms, deliberately separate:

  * `classify()` and `ingest()` work on parsed messages and know nothing about
    IMAP. They are pure and fully tested.
  * `connect()` is the thin, untestable-without-credentials part.

An opt-out is acted on the moment it is seen — `suppress()` runs during
ingestion, not in a report a human is supposed to read later. A removal that
waits for someone to notice is the failure this whole module exists to close.
"""
from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from . import storage
from .config import settings

# Phrases that mean "stop emailing me". Matched against subject and the first
# part of the body only: further down lies the quoted original, which contains
# our own footer and therefore the word "unsubscribe" every single time.
OPT_OUT = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[- ]?out|stop emailing|do not (?:contact|email))\b",
    re.I,
)

# Automatic replies. Not a human, must not count as a reply, must not be
# suppressed — an out-of-office is not an opt-out.
AUTO = re.compile(
    r"\b(out of (?:the )?office|auto[- ]?repl|automatic reply|vacation|"
    r"annual leave|parental leave|on holiday|away from my desk)\b", re.I)

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "no-reply@dmarcreport")
BOUNCE_SUBJECTS = re.compile(
    r"(undeliverable|undelivered mail|delivery status notification|"
    r"returned to sender|delivery has failed|address not found)", re.I)

BODY_HEAD_CHARS = 600


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_head(msg: Message) -> str:
    """First readable text, truncated. Never stored — only classified on."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode("utf-8", "replace")[:BODY_HEAD_CHARS]
            return ""
        payload = msg.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")[:BODY_HEAD_CHARS]
    except Exception:
        return ""


def classify(msg: Message) -> str:
    """bounce | auto | unsubscribe | human.

    Order matters. A bounce can quote our footer, so bounce is decided first;
    an auto-reply can quote it too, so auto comes before opt-out. Only what is
    left is treated as a person asking to be removed.
    """
    sender = parseaddr(_decode(msg.get("From")))[1].lower()
    subject = _decode(msg.get("Subject"))

    if any(s in sender for s in BOUNCE_SENDERS) or BOUNCE_SUBJECTS.search(subject):
        return "bounce"
    if msg.get("Auto-Submitted", "").lower().startswith("auto") or AUTO.search(subject):
        return "auto"
    if OPT_OUT.search(subject) or OPT_OUT.search(_body_head(msg)):
        return "unsubscribe"
    return "human"


def ingest(msg: Message) -> dict:
    """Record one inbound message, and honour it if it is an opt-out.

    Returns what happened, so the caller can print a line per message rather
    than a total that hides a suppression failure inside a success count.
    """
    sender = parseaddr(_decode(msg.get("From")))[1].lower()
    subject = _decode(msg.get("Subject"))[:300] or None
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    message_id = (msg.get("Message-ID") or "").strip() or None
    kind = classify(msg)

    try:
        received = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        received = None

    send = storage.find_send_by_message_id(in_reply_to)
    send_id = send["id"] if send else None

    reply_id = storage.record_reply(
        from_email=sender, subject=subject, kind=kind, send_id=send_id,
        in_reply_to=in_reply_to, message_id=message_id, received_at=received,
    )
    if reply_id is None:
        return {"kind": kind, "from": sender, "status": "already_ingested"}

    if kind == "bounce" and send_id:
        storage.set_delivery_status(send_id, "bounced")

    suppressed = False
    if kind in ("unsubscribe", "bounce"):
        # The address the reply came from is the one to honour; a bounce is
        # about the address we wrote to, which is the send's recipient.
        target = send["to_email"] if (kind == "bounce" and send) else sender
        if target:
            storage.suppress(email=target, reason=kind, source="inbox",
                             note=subject)
            suppressed = True

    return {"kind": kind, "from": sender, "send_id": send_id,
            "suppressed": suppressed, "status": "ingested"}


def connect(folder: str = "INBOX") -> imaplib.IMAP4_SSL:
    """Open the mailbox replies land in. Credentials from env only."""
    if not (settings.imap_user and settings.imap_password):
        raise RuntimeError(
            "IMAP is not configured. Set IMAP_USER and IMAP_PASSWORD (a Google "
            "Workspace app password, not the account password)."
        )
    client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    client.login(settings.imap_user, settings.imap_password)
    client.select(folder)
    return client


def fetch_recent(client, days: int = 7) -> list[Message]:
    """Everything in the window, read-only.

    Deliberately not `UNSEEN`: the seen flag belongs to whoever opens the
    mailbox on their phone, and an opt-out that was read on a phone is exactly
    the one that must not be missed. Re-ingestion is safe — `record_reply` is
    idempotent on Message-ID.
    """
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    typ, data = client.search(None, f'(SINCE {since})')
    if typ != "OK" or not data or not data[0]:
        return []
    out = []
    for num in data[0].split():
        typ, raw = client.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not raw or not raw[0]:
            continue
        out.append(email.message_from_bytes(raw[0][1]))
    return out
