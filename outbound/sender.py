"""Send mail via Resend (preferred) or SMTP. Returns provider + message id."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import httpx

from .config import DRYRUN_PROVIDER, settings


class TransportRejected(RuntimeError):
    """Positive evidence the message was NOT accepted.

    The request was refused before delivery was attempted — a malformed
    address, a rejected sender, a rate limit. Safe to free the slot and try
    the lead again another day.
    """


class TransportAmbiguous(RuntimeError):
    """The outcome is unknown: timeout, 5xx, dropped connection.

    The message may well have gone out. Treated as contact, because the cost
    of assuming otherwise is a duplicate email — the one failure in this
    system a recipient can see.
    """


class IdentityMissing(RuntimeError):
    """No registered postal address configured, so no lawful send is possible."""


#: CAN-SPAM §316.3(a)(1) requires a commercial message to identify itself as a
#: solicitation, clearly and conspicuously, unless the recipient consented in
#: advance — and none of ours have. It sits immediately above the opt-out so
#: the two read as one block: this is an ad, here is how to stop it. Saying it
#: plainly also costs nothing we want to keep; the pitch is that we do not
#: overstate things.
AD_IDENTIFICATION = "This is a commercial email you didn't ask for."


def _unsubscribe_line() -> str:
    return (
        f"Don't want these? Reply 'unsubscribe' and you're off the list for good, "
        f"or mail {settings.unsubscribe_email} with 'unsubscribe' in the subject."
    )


def _with_footer(body: str) -> str:
    """Append sender identity, opt-out, and where the data came from.

    Raises IdentityMissing when SENDER_POSTAL_ADDRESS is unset. A commercial
    email to a named person has to carry the sender's registered identity —
    GDPR Art. 13/14, CAN-SPAM, and every national marketing rule agree on that
    much. Sending without it would be the exact shape of failure this repo
    exists to catch: the run reports success and every message it produced was
    unlawful. Refusing loudly costs one env var; the alternative costs the
    domain. Dry runs are exempt: a rehearsal has to work before the identity
    is decided, and it reaches nobody.
    """
    if not settings.dry_run and not settings.sender_postal_address.strip():
        raise IdentityMissing(
            "SENDER_POSTAL_ADDRESS is not set. A real send needs the controller's "
            "registered name and postal address in the footer; refusing to send "
            "without it. Set it in .env, or run with DRY_RUN=true."
        )
    address = settings.sender_postal_address.strip() or "[SENDER_POSTAL_ADDRESS unset — dry run]"
    sig = (
        f"\n\n— {settings.sender_name}\n"
        f"{settings.sender_company} · {settings.sender_booking_url}\n"
        f"{address}\n"
    )
    return (
        f"{body.rstrip()}{sig}\n"
        f"{AD_IDENTIFICATION}\n"
        f"{_unsubscribe_line()}\n"
        f"How we found you and what we hold: {settings.privacy_policy_url}\n"
    )


def _unsubscribe_headers() -> dict:
    """RFC 8058 one-click-ish opt-out. Mailto only: a URL would need an
    endpoint that actually removes the address, and we don't have one."""
    return {
        "List-Unsubscribe": f"<mailto:{settings.unsubscribe_email}?subject=unsubscribe>",
    }


def send_email(to_email: str, subject: str, body: str) -> dict:
    """Send a plaintext email. Returns {provider, message_id, dry_run}.

    If DRY_RUN=true in env, doesn't actually send — returns a stub.
    """
    body = _with_footer(body)

    if settings.dry_run:
        return {"provider": DRYRUN_PROVIDER, "message_id": None, "dry_run": True,
                "preview": {"to": to_email, "subject": subject, "body": body}}

    if settings.resend_api_key:
        return _send_resend(to_email, subject, body)
    if settings.smtp_user and settings.smtp_password:
        return _send_smtp(to_email, subject, body)
    raise RuntimeError(
        "No email transport configured. Set RESEND_API_KEY or SMTP_USER/SMTP_PASSWORD."
    )


def _send_resend(to_email: str, subject: str, body: str) -> dict:
    payload = {
        "from": f"{settings.sender_name} <{settings.sender_email}>",
        "to": [to_email],
        "subject": subject,
        "text": body,
        "reply_to": settings.sender_reply_to,
        "headers": _unsubscribe_headers(),
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
    except httpx.RequestError as exc:
        # Never reached the API, or reached it and we never heard back.
        raise TransportAmbiguous(f"resend request failed: {exc}") from exc

    if r.status_code >= 500:
        raise TransportAmbiguous(f"resend {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        # The API refused the request; nothing was queued.
        raise TransportRejected(f"resend {r.status_code}: {r.text[:200]}")

    data = r.json()
    return {"provider": "resend", "message_id": data.get("id"), "dry_run": False}


def _send_smtp(to_email: str, subject: str, body: str) -> dict:
    msg = EmailMessage()
    msg["From"] = f"{settings.sender_name} <{settings.sender_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = settings.sender_reply_to
    for key, value in _unsubscribe_headers().items():
        msg[key] = value

    # Generate the id ourselves, BEFORE sending. Neither EmailMessage nor
    # smtplib.send_message() sets Message-ID — the receiving MTA does, and we
    # never see it. Reading msg["Message-ID"] after the send returns None
    # every time, which silently leaves every SMTP row uncorrelatable.
    domain = settings.sender_email.rpartition("@")[2] or None
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
            smtplib.SMTPNotSupportedError, smtplib.SMTPAuthenticationError) as exc:
        raise TransportRejected(f"smtp refused: {exc}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        # Disconnects and timeouts can happen after DATA was accepted.
        raise TransportAmbiguous(f"smtp failed: {exc}") from exc

    return {"provider": "smtp", "message_id": message_id, "dry_run": False}
