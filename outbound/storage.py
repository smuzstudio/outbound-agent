"""SQLite persistence: leads + sends with dedupe and daily-cap accounting."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DRYRUN_PROVIDER, settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                -- 'yc' | 'producthunt'
    source_ref TEXT NOT NULL,            -- e.g. yc slug or PH post id
    company_name TEXT NOT NULL,
    company_url TEXT,
    company_domain TEXT,
    founder_name TEXT,
    founder_email TEXT,
    one_liner TEXT,
    research_notes TEXT,                 -- JSON string with extracted signals
    status TEXT NOT NULL DEFAULT 'new',  -- new | researched | drafted | sent | skipped | bounced
    skip_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(company_domain);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(founder_email);

CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    provider TEXT NOT NULL,              -- 'resend' | 'smtp' | 'dryrun'
    provider_message_id TEXT,
    sent_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sends_day ON sends(sent_at);
CREATE INDEX IF NOT EXISTS idx_sends_email ON sends(to_email);

-- The run ledger. Without it, a day on which cron never fired is
-- indistinguishable from a day with no qualifying leads: both leave the
-- database exactly as it was. Every liveness and throughput assertion reads
-- from here.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source TEXT NOT NULL,
    requested_n INTEGER NOT NULL,
    dry_run INTEGER NOT NULL,            -- 0 | 1, as configured at start
    daily_cap INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    cost_usd REAL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

-- Opt-outs, and every other address we must never contact again.
--
-- The footer has promised "reply unsubscribe and we'll remove you" since the
-- first version, with nothing behind it: the promise was kept by memory, and
-- memory is not a mechanism. An opt-out that depends on someone remembering
-- is the same failure class this repo audits itself for — a step that reports
-- success while doing nothing.
--
-- Suppression is by address AND by domain, because an opt-out from one person
-- at a company is a signal about the company, not just the inbox. Rows are
-- never deleted on removal: `active = 0` keeps the record that the request was
-- made and honoured, which is the only thing that can be shown to a regulator.
CREATE TABLE IF NOT EXISTS suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT,                          -- lowercased, or NULL for a domain-wide entry
    domain TEXT,                         -- lowercased
    reason TEXT NOT NULL,                -- unsubscribe | bounce | complaint | manual | jurisdiction
    source TEXT,                         -- who/what added it, free text
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_suppressions_email
    ON suppressions(email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_suppressions_domain
    ON suppressions(domain) WHERE domain IS NOT NULL AND email IS NULL;

-- What came back. Four numbers are worth having — delivered, bounced, replied,
-- and of those replies how many were a person rather than a robot — and none
-- of them can be read from `sends`, which only records that we handed a message
-- to a transport.
--
-- Deliberately NOT here: open tracking. A pixel needs consent under ePrivacy,
-- Apple Mail Privacy Protection makes the number fiction, and remote images
-- cost deliverability. A metric that is both unlawful and wrong is worse than
-- no metric.
--
-- Message bodies are not stored. A reply is a named person's words about their
-- own systems; the classification and the fact of it are enough to operate on,
-- and every field kept here is a field that has to be defended later.
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    send_id INTEGER REFERENCES sends(id),   -- NULL when it matches nothing we sent
    from_email TEXT NOT NULL,
    subject TEXT,
    kind TEXT NOT NULL,                     -- human | bounce | auto | unsubscribe
    in_reply_to TEXT,
    message_id TEXT,
    received_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_replies_message_id
    ON replies(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_replies_send ON replies(send_id);
"""

# Columns added after the first release. Stamped onto leads and sends so that
# per-run throughput is *derived by counting rows*, never read from a counter
# the run reported about itself. A run that lies about its own numbers is the
# failure this ledger exists to catch.
MIGRATIONS = [
    ("leads", "run_id", "ALTER TABLE leads ADD COLUMN run_id INTEGER"),
    ("sends", "run_id", "ALTER TABLE sends ADD COLUMN run_id INTEGER"),
    # 'sent' is the right default for rows written before this column existed:
    # every one of them was recorded after a transport call returned.
    ("sends", "status", "ALTER TABLE sends ADD COLUMN status TEXT DEFAULT 'sent'"),
    ("sends", "error", "ALTER TABLE sends ADD COLUMN error TEXT"),
    # ISO-3166 alpha-2, or NULL when discovery could not determine it. NULL is
    # a refusal at send time, not a shrug: see jurisdiction.py.
    ("leads", "country", "ALTER TABLE leads ADD COLUMN country TEXT"),
    # What the transport said afterwards: delivered | bounced | complained.
    # NULL means nobody has asked yet, which is different from "fine".
    ("sends", "delivery_status", "ALTER TABLE sends ADD COLUMN delivery_status TEXT"),
    ("sends", "delivery_checked_at", "ALTER TABLE sends ADD COLUMN delivery_checked_at TEXT"),
]

# A send is claimed before the transport call and confirmed after it. These
# two states both count as contact; only RELEASED does not.
#
#   RESERVED — claimed, outcome unknown. Counts. A row stuck here means the
#              process died between claiming and confirming, and we must
#              assume the mail went out: emailing someone twice is visible to
#              them, and never emailing them again is not.
#   SENT     — transport accepted it.
#   RELEASED — transport gave positive evidence of rejection. Only then is it
#              safe to free the slot and let the lead be tried again.
SEND_RESERVED = "reserved"
SEND_SENT = "sent"
SEND_RELEASED = "released"

# Statuses that count as having contacted someone.
CONTACT_STATUSES = (SEND_RESERVED, SEND_SENT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(settings.db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                con.execute(ddl)


# The run this process is executing, set by start_run(). Single-process by
# design: the CLI is one run per invocation. Concurrent runs are prevented by
# the lockfile, not by this variable.
_CURRENT_RUN_ID: int | None = None


def start_run(*, source: str, requested_n: int, dry_run: bool, daily_cap: int) -> int:
    """Open a run row before any work happens, so a crash still leaves evidence.

    Written first and updated last. A row with status='running' and no
    finished_at is itself a finding: the process died mid-run.
    """
    global _CURRENT_RUN_ID
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO runs (started_at, source, requested_n, dry_run,
                                 daily_cap, status)
               VALUES (?, ?, ?, ?, ?, 'running')""",
            (_now(), source, requested_n, int(dry_run), daily_cap),
        )
        _CURRENT_RUN_ID = int(cur.lastrowid)
    return _CURRENT_RUN_ID


def finish_run(
    run_id: int | None = None, *, status: str = "ok",
    cost_usd: float | None = None, error: str | None = None,
) -> None:
    run_id = run_id if run_id is not None else _CURRENT_RUN_ID
    if run_id is None:
        return
    with _conn() as con:
        con.execute(
            """UPDATE runs SET finished_at = ?, status = ?, cost_usd = ?, error = ?
               WHERE id = ?""",
            (_now(), status, cost_usd, error, run_id),
        )


def current_run_id() -> int | None:
    return _CURRENT_RUN_ID


def run_counts(run_id: int) -> dict[str, int]:
    """Throughput for a run, counted from rows — not from anything self-reported."""
    with _conn() as con:
        row = con.execute(
            """SELECT
                 COUNT(*) AS discovered,
                 SUM(status = 'sent') AS sent,
                 SUM(status = 'skipped') AS skipped,
                 SUM(status NOT IN ('sent', 'skipped')) AS pending
               FROM leads WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        return {k: int(row[k] or 0) for k in ("discovered", "sent", "skipped", "pending")}


def upsert_lead(
    *,
    source: str,
    source_ref: str,
    company_name: str,
    company_url: str | None = None,
    company_domain: str | None = None,
    founder_name: str | None = None,
    founder_email: str | None = None,
    one_liner: str | None = None,
    country: str | None = None,
) -> int | None:
    """Insert a lead. Returns lead_id, or None if it already existed."""
    with _conn() as con:
        cur = con.execute(
            "SELECT id FROM leads WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        )
        if cur.fetchone():
            return None
        cur = con.execute(
            """
            INSERT INTO leads (source, source_ref, company_name, company_url,
                               company_domain, founder_name, founder_email,
                               one_liner, country, status, created_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (source, source_ref, company_name, company_url, company_domain,
             founder_name, founder_email, one_liner, country, _now(),
             _CURRENT_RUN_ID),
        )
        return int(cur.lastrowid)


def update_lead(lead_id: int, **fields: Any) -> None:
    if not fields:
        return
    if "research_notes" in fields and not isinstance(fields["research_notes"], str):
        fields["research_notes"] = json.dumps(fields["research_notes"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as con:
        con.execute(f"UPDATE leads SET {sets} WHERE id = ?", (*fields.values(), lead_id))


def get_lead(lead_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def list_leads(status: str | None = None, limit: int = 50) -> list[dict]:
    with _conn() as con:
        if status:
            rows = con.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def already_contacted(email: str | None, domain: str | None) -> bool:
    """True if we've ever really emailed this address OR anyone at this domain.

    Dry-run rows are ignored: a rehearsal must never make a lead unreachable.
    """
    if not email and not domain:
        return False
    with _conn() as con:
        if email:
            row = con.execute(
                """SELECT 1 FROM sends
                   WHERE LOWER(to_email) = LOWER(?) AND provider != ?
                     AND status IN (?, ?) LIMIT 1""",
                (email, DRYRUN_PROVIDER, *CONTACT_STATUSES),
            ).fetchone()
            if row:
                return True
        if domain:
            row = con.execute(
                """SELECT 1 FROM sends s JOIN leads l ON s.lead_id = l.id
                   WHERE LOWER(l.company_domain) = LOWER(?) AND s.provider != ?
                     AND s.status IN (?, ?) LIMIT 1""",
                (domain, DRYRUN_PROVIDER, *CONTACT_STATUSES),
            ).fetchone()
            if row:
                return True
    return False


# --- Suppression -------------------------------------------------------------
#
# Checked before the cap is claimed, not after. A suppressed address must not
# consume a slot: spending today's cap discovering that fifteen people opted
# out would make the opt-out list look like a delivery failure.

SUPPRESSION_REASONS = ("unsubscribe", "bounce", "complaint", "manual", "jurisdiction")


def suppress(
    *, email: str | None = None, domain: str | None = None, reason: str = "manual",
    source: str | None = None, note: str | None = None,
) -> int:
    """Record an address or a whole domain as never-contact. Idempotent."""
    if not email and not domain:
        raise ValueError("suppress() needs an email or a domain")
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(f"unknown reason {reason!r}; expected one of {SUPPRESSION_REASONS}")
    email = email.strip().lower() if email else None
    domain = domain.strip().lower() if domain else None
    if email and not domain:
        domain = email.rpartition("@")[2] or None
    # A domain-wide entry carries no email; an address entry carries both, and
    # the partial unique index above keeps the two kinds from colliding.
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM suppressions WHERE email IS ? AND domain IS ?"
            if email else
            "SELECT id FROM suppressions WHERE email IS NULL AND domain IS ?",
            (email, domain) if email else (domain,),
        ).fetchone()
        if row:
            con.execute(
                "UPDATE suppressions SET active = 1, reason = ?, source = ?, note = ? WHERE id = ?",
                (reason, source, note, row["id"]),
            )
            return int(row["id"])
        cur = con.execute(
            """INSERT INTO suppressions (email, domain, reason, source, note, active, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (email, domain, reason, source, note, _now()),
        )
        return int(cur.lastrowid)


def unsuppress(*, email: str | None = None, domain: str | None = None) -> int:
    """Deactivate an entry, keeping the row. Returns rows affected."""
    email = email.strip().lower() if email else None
    domain = domain.strip().lower() if domain else None
    with _conn() as con:
        if email:
            cur = con.execute(
                "UPDATE suppressions SET active = 0 WHERE email = ?", (email,))
        else:
            cur = con.execute(
                "UPDATE suppressions SET active = 0 WHERE domain = ? AND email IS NULL",
                (domain,))
        return int(cur.rowcount)


def is_suppressed(email: str | None, domain: str | None = None) -> dict | None:
    """Return the matching active suppression, or None.

    An address matches its own entry or any domain-wide entry. The domain is
    derived from the address when not given, so a caller cannot bypass a
    domain block by passing the address alone.
    """
    email = email.strip().lower() if email else None
    domain = (domain or "").strip().lower() or None
    if email and not domain:
        domain = email.rpartition("@")[2] or None
    if not email and not domain:
        return None
    with _conn() as con:
        if email:
            row = con.execute(
                "SELECT * FROM suppressions WHERE active = 1 AND email = ? LIMIT 1",
                (email,),
            ).fetchone()
            if row:
                return dict(row)
        if domain:
            row = con.execute(
                """SELECT * FROM suppressions
                   WHERE active = 1 AND email IS NULL AND domain = ? LIMIT 1""",
                (domain,),
            ).fetchone()
            if row:
                return dict(row)
    return None


def list_suppressions(active_only: bool = True, limit: int = 500) -> list[dict]:
    with _conn() as con:
        sql = "SELECT * FROM suppressions"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in con.execute(sql, (limit,)).fetchall()]


# --- What came back ----------------------------------------------------------

def find_send_by_message_id(message_id: str | None) -> dict | None:
    """Match an inbound In-Reply-To header back to the send it answers."""
    if not message_id:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM sends WHERE provider_message_id = ? LIMIT 1",
            (message_id.strip(),),
        ).fetchone()
        return dict(row) if row else None


def record_reply(
    *, from_email: str, kind: str, subject: str | None = None,
    send_id: int | None = None, in_reply_to: str | None = None,
    message_id: str | None = None, received_at: str | None = None,
) -> int | None:
    """Store one inbound message. Returns None if we already had it.

    Idempotent on Message-ID so that re-running ingestion over the same mailbox
    window — which is the normal case, since IMAP flags are not ours to trust —
    cannot double-count replies or re-open a closed thread.
    """
    with _conn() as con:
        if message_id:
            row = con.execute(
                "SELECT id FROM replies WHERE message_id = ?", (message_id,)
            ).fetchone()
            if row:
                return None
        cur = con.execute(
            """INSERT INTO replies (send_id, from_email, subject, kind, in_reply_to,
                                    message_id, received_at, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (send_id, from_email.strip().lower(), subject, kind, in_reply_to,
             message_id, received_at or _now(), _now()),
        )
        return int(cur.lastrowid)


def set_delivery_status(send_id: int, status: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE sends SET delivery_status = ?, delivery_checked_at = ? WHERE id = ?",
            (status, _now(), send_id),
        )


def sends_awaiting_delivery_status(provider: str = "resend", limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, provider_message_id, sent_at FROM sends
               WHERE provider = ? AND status = ? AND provider_message_id IS NOT NULL
                 AND (delivery_status IS NULL OR delivery_status = 'queued')
               ORDER BY id DESC LIMIT ?""",
            (provider, SEND_SENT, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def outbound_stats(days: int = 30) -> dict:
    """The four numbers worth reading, plus the two that qualify them.

    Rates are over *delivered* where the denominator is knowable and over sent
    otherwise, and the dict says which — a reply rate quoted against sends when
    a tenth bounced is a flattering number, and flattering numbers are how a
    channel gets kept alive past the point it should have been cut.
    """
    with _conn() as con:
        sent = con.execute(
            """SELECT COUNT(*) FROM sends
               WHERE provider != ? AND status IN (?, ?)
                 AND sent_at >= date('now', ?)""",
            (DRYRUN_PROVIDER, *CONTACT_STATUSES, f"-{int(days)} days"),
        ).fetchone()[0]
        by_delivery = dict(con.execute(
            """SELECT COALESCE(delivery_status, 'unknown'), COUNT(*) FROM sends
               WHERE provider != ? AND status IN (?, ?)
                 AND sent_at >= date('now', ?)
               GROUP BY 1""",
            (DRYRUN_PROVIDER, *CONTACT_STATUSES, f"-{int(days)} days"),
        ).fetchall())
        by_kind = dict(con.execute(
            "SELECT kind, COUNT(*) FROM replies WHERE received_at >= date('now', ?) GROUP BY 1",
            (f"-{int(days)} days",),
        ).fetchall())
        suppressed = con.execute(
            "SELECT COUNT(*) FROM suppressions WHERE active = 1").fetchone()[0]

    bounced = int(by_delivery.get("bounced", 0)) + int(by_kind.get("bounce", 0))
    human = int(by_kind.get("human", 0)) + int(by_kind.get("unsubscribe", 0))
    delivered_known = int(by_delivery.get("delivered", 0))
    return {
        "days": days,
        "sent": sent,
        "delivered_confirmed": delivered_known,
        "bounced": bounced,
        "replies_human": int(by_kind.get("human", 0)),
        "replies_unsubscribe": int(by_kind.get("unsubscribe", 0)),
        "replies_auto": int(by_kind.get("auto", 0)),
        "suppressions_active": suppressed,
        "bounce_rate_of_sent": round(bounced / sent, 4) if sent else None,
        "reply_rate_of_sent": round(human / sent, 4) if sent else None,
        # Stated so nobody quotes a rate whose denominator was never measured.
        "delivery_confirmed_for": f"{delivered_known}/{sent}" if sent else "0/0",
    }


class CapReached(RuntimeError):
    """The daily cap was already met when this send tried to claim a slot."""


def reserve_send(
    *, lead_id: int, to_email: str, subject: str, body: str, provider: str,
    daily_cap: int,
) -> int:
    """Claim one slot under the daily cap and return the send id.

    The count and the insert happen inside a single BEGIN IMMEDIATE
    transaction. This is the whole point: reading the count in one
    transaction and inserting in another — which is what a check-then-act
    cap does — lets two concurrent runs both observe `n < cap` and both
    proceed. BEGIN IMMEDIATE takes the write lock up front, so the second
    process waits rather than races.

    Raises CapReached rather than returning None so that a caller cannot
    ignore the result by accident.
    """
    con = sqlite3.connect(settings.db_path, timeout=30.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            if provider != DRYRUN_PROVIDER:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                row = con.execute(
                    """SELECT COUNT(*) AS n FROM sends
                       WHERE substr(sent_at, 1, 10) = ? AND provider != ?
                         AND status IN (?, ?)""",
                    (today, DRYRUN_PROVIDER, *CONTACT_STATUSES),
                ).fetchone()
                if int(row["n"]) >= daily_cap:
                    raise CapReached(f"daily cap reached: {row['n']}/{daily_cap}")
            cur = con.execute(
                """INSERT INTO sends (lead_id, to_email, subject, body, provider,
                                      provider_message_id, sent_at, run_id, status)
                   VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (lead_id, to_email, subject, body, provider, _now(),
                 _CURRENT_RUN_ID, SEND_RESERVED),
            )
            send_id = int(cur.lastrowid)
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        return send_id
    finally:
        con.close()


def confirm_send(send_id: int, *, provider: str, provider_message_id: str | None) -> None:
    """The transport accepted it. Records which transport, now that we know."""
    with _conn() as con:
        con.execute(
            """UPDATE sends SET status = ?, provider = ?, provider_message_id = ?
               WHERE id = ?""",
            (SEND_SENT, provider, provider_message_id, send_id),
        )


def release_send(send_id: int, *, error: str) -> None:
    """The transport positively rejected it — free the slot.

    Call this ONLY with evidence the message was not accepted. For an
    ambiguous outcome (timeout, 5xx, dropped connection) leave the row
    reserved and use `record_send_error`: an unnecessary skip is invisible,
    a duplicate email is not.
    """
    with _conn() as con:
        con.execute(
            "UPDATE sends SET status = ?, error = ? WHERE id = ?",
            (SEND_RELEASED, error, send_id),
        )


def record_send_error(send_id: int, *, error: str) -> None:
    """Ambiguous transport outcome. The row stays reserved and keeps counting."""
    with _conn() as con:
        con.execute("UPDATE sends SET error = ? WHERE id = ?", (error, send_id))


def sends_today() -> int:
    """Real sends today. Dry runs don't consume the cap."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as con:
        row = con.execute(
            """SELECT COUNT(*) AS n FROM sends
               WHERE substr(sent_at, 1, 10) = ? AND provider != ?
                 AND status IN (?, ?)""",
            (today, DRYRUN_PROVIDER, *CONTACT_STATUSES),
        ).fetchone()
        return int(row["n"]) if row else 0
