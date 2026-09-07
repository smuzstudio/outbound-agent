"""Tests for refusing to send to an address we only guessed.

`find_email()` grades every address it returns, and for a long time that grade
was computed, stored and never read: a pattern-guessed `hello@{domain}` left
the building exactly like a Hunter-verified hit. These tests exist so the grade
keeps being *load-bearing* rather than decorative.

`test_ungraded_lead_is_refused` is the one that matters most. Failing open on a
missing grade would protect only the leads that happened to go through
research — the same shape of hole as a jurisdiction gate that shrugs at an
unknown country.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConfidenceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = str(Path(self.tmp.name) / "outbound.db")
        os.environ["DRY_RUN"] = "true"
        os.environ["MARKETING_EXCLUDED_COUNTRIES"] = "PL,DE,AT"
        for mod in [m for m in list(sys.modules) if m.startswith("outbound")]:
            del sys.modules[mod]
        sys.path.insert(0, str(ROOT))
        from outbound import storage, tools
        self.storage, self.tools = storage, tools
        storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _lead(self, confidence: str | None) -> int:
        lead_id = self.storage.upsert_lead(
            source="targets", source_ref=f"acme-{confidence}",
            company_name="Acme", company_url="https://acme.com",
            company_domain="acme.com", founder_email="dana@acme.com",
            country="US",
        )
        if confidence is not None:
            self.storage.update_lead(lead_id, email_confidence=confidence)
        return lead_id

    def _send(self, lead_id: int) -> dict:
        out = asyncio.run(self.tools.send_email_tool.handler(
            {"lead_id": lead_id, "subject": "s", "body": "b"}))
        return json.loads(out["content"][0]["text"])

    def test_guessed_address_is_refused(self) -> None:
        r = self._send(self._lead("guess"))
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r["reason"], "email_unverified")

    def test_ungraded_lead_is_refused(self) -> None:
        """No grade at all is a refusal, not a shrug."""
        r = self._send(self._lead(None))
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r["reason"], "email_ungraded")

    def test_verified_address_is_allowed_through(self) -> None:
        for grade in ("hunter", "site", "source"):
            with self.subTest(grade=grade):
                r = self._send(self._lead(grade))
                self.assertNotIn(r.get("reason"),
                                 ("email_unverified", "email_ungraded"))

    def test_refusal_does_not_consume_the_daily_cap(self) -> None:
        """A refused lead must not spend a slot.

        If it did, a day of guessed addresses would look identical in the
        numbers to a day of real sends that all failed.
        """
        before = self.storage.sends_today()
        self._send(self._lead("guess"))
        self.assertEqual(self.storage.sends_today(), before)


if __name__ == "__main__":
    unittest.main()
