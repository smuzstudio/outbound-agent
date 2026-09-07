"""Tests for reading what came back.

The classifier is the part that can quietly go wrong: everything it misreads
either loses an opt-out or suppresses someone who only turned on their
out-of-office. Both are silent, which is the class of failure this repo exists
to catch, so each test breaks one specific misreading.
"""
from __future__ import annotations

import email
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload(*names):
    from outbound import config
    importlib.reload(config)
    mods = [importlib.reload(importlib.import_module(f"outbound.{n}")) for n in names]
    return mods[0] if len(mods) == 1 else mods


def _msg(frm="ada@acme.com", subject="re: silent failures", body="sure, let's talk",
         in_reply_to=None, message_id="<m1@smuz.io>", extra=None) -> email.message.Message:
    headers = [f"From: {frm}", f"Subject: {subject}", f"Message-ID: {message_id}",
               "Date: Mon, 7 Sep 2026 09:00:00 +0000"]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    for k, v in (extra or {}).items():
        headers.append(f"{k}: {v}")
    return email.message_from_string("\n".join(headers) + f"\n\n{body}\n")


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = str(Path(self.tmp.name) / "t.db")
        self.storage, self.inbox = _reload("storage", "inbox")
        self.storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_person_replying_is_human(self) -> None:
        self.assertEqual(self.inbox.classify(_msg()), "human")

    def test_opt_out_in_the_subject(self) -> None:
        self.assertEqual(self.inbox.classify(_msg(subject="unsubscribe")), "unsubscribe")

    def test_opt_out_phrased_in_english(self) -> None:
        for phrase in ("please remove me", "take me off this list",
                       "opt out", "stop emailing me", "do not contact me again"):
            self.assertEqual(self.inbox.classify(_msg(body=phrase)), "unsubscribe", phrase)

    def test_out_of_office_is_not_an_opt_out(self) -> None:
        """The expensive misread: suppressing someone who went on holiday."""
        m = _msg(subject="Out of office: re: silent failures")
        self.assertEqual(self.inbox.classify(m), "auto")

    def test_auto_submitted_header_is_respected(self) -> None:
        m = _msg(subject="Re: hello", extra={"Auto-Submitted": "auto-replied"})
        self.assertEqual(self.inbox.classify(m), "auto")

    def test_bounce_from_mailer_daemon(self) -> None:
        m = _msg(frm="MAILER-DAEMON@googlemail.com",
                 subject="Delivery Status Notification (Failure)")
        self.assertEqual(self.inbox.classify(m), "bounce")

    def test_our_own_footer_quoted_back_is_not_an_opt_out(self) -> None:
        """Every reply quotes the original, and the original says 'unsubscribe'.

        Reading the whole body would classify every polite reply as a removal
        request and empty the list within a week.
        """
        quoted = ("Happy to chat next week.\n\n" + "x" * 700 +
                  "\n> Don't want these? Reply 'unsubscribe' and you're off the list")
        self.assertEqual(self.inbox.classify(_msg(body=quoted)), "human")


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = str(Path(self.tmp.name) / "t.db")
        self.storage, self.inbox = _reload("storage", "inbox")
        self.storage.init_db()
        self.storage.upsert_lead(source="yc", source_ref="acme", company_name="Acme",
                                 company_domain="acme.com", founder_email="ada@acme.com")
        self.send_id = self.storage.reserve_send(
            lead_id=1, to_email="ada@acme.com", subject="s", body="b",
            provider="pending", daily_cap=10)
        self.storage.confirm_send(self.send_id, provider="smtp",
                                  provider_message_id="<out1@smuz.io>")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reply_is_matched_back_to_the_send(self) -> None:
        r = self.inbox.ingest(_msg(in_reply_to="<out1@smuz.io>"))
        self.assertEqual(r["send_id"], self.send_id)

    def test_optout_suppresses_immediately(self) -> None:
        """Acted on during ingestion, not written into a report someone reads later."""
        r = self.inbox.ingest(_msg(subject="unsubscribe", in_reply_to="<out1@smuz.io>"))
        self.assertTrue(r["suppressed"])
        self.assertIsNotNone(self.storage.is_suppressed("ada@acme.com"))

    def test_bounce_suppresses_the_address_we_wrote_to(self) -> None:
        m = _msg(frm="mailer-daemon@google.com", subject="Undelivered Mail Returned to Sender",
                 in_reply_to="<out1@smuz.io>")
        r = self.inbox.ingest(m)
        self.assertTrue(r["suppressed"])
        self.assertIsNotNone(self.storage.is_suppressed("ada@acme.com"))
        self.assertIsNone(self.storage.is_suppressed("mailer-daemon@google.com"))

    def test_bounce_marks_the_send_undelivered(self) -> None:
        self.inbox.ingest(_msg(frm="postmaster@acme.com", subject="Undeliverable",
                               in_reply_to="<out1@smuz.io>"))
        with self.storage._conn() as con:
            row = con.execute("SELECT delivery_status FROM sends WHERE id = ?",
                              (self.send_id,)).fetchone()
        self.assertEqual(row["delivery_status"], "bounced")

    def test_out_of_office_does_not_suppress(self) -> None:
        r = self.inbox.ingest(_msg(subject="Out of office", in_reply_to="<out1@smuz.io>"))
        self.assertFalse(r["suppressed"])
        self.assertIsNone(self.storage.is_suppressed("ada@acme.com"))

    def test_reingesting_the_same_message_is_a_no_op(self) -> None:
        """Ingestion re-reads the whole window every time, by design."""
        self.inbox.ingest(_msg(message_id="<dup@x>"))
        again = self.inbox.ingest(_msg(message_id="<dup@x>"))
        self.assertEqual(again["status"], "already_ingested")
        with self.storage._conn() as con:
            n = con.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
        self.assertEqual(n, 1)

    def test_stats_report_what_was_actually_measured(self) -> None:
        self.inbox.ingest(_msg(in_reply_to="<out1@smuz.io>", message_id="<r1@x>"))
        st = self.storage.outbound_stats(30)
        self.assertEqual(st["sent"], 1)
        self.assertEqual(st["replies_human"], 1)
        # Nothing has confirmed delivery, and the report must say so rather
        # than implying the send landed.
        self.assertEqual(st["delivery_confirmed_for"], "0/1")


if __name__ == "__main__":
    unittest.main()
