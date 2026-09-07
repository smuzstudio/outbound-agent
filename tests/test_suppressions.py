"""Tests for the opt-out mechanism.

The footer has promised removal since the first version. These tests exist to
make that promise checkable: each one breaks a specific way of failing to
honour it, and a promise no test can break is the thing this repo audits for.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload(*names):
    """Re-import modules so they see the current environment.

    `sys.modules.pop()` alone is not enough: the parent package still holds the
    old module as an attribute, so `from outbound import storage` hands back the
    stale one — pointing at a deleted temp database, or at settings frozen
    before the test set them.
    """
    from outbound import config
    importlib.reload(config)
    out = []
    for name in names:
        mod = importlib.import_module(f"outbound.{name}")
        out.append(importlib.reload(mod))
    return out[0] if len(out) == 1 else out


class SuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = str(Path(self.tmp.name) / "t.db")
        self.storage = _reload("storage")
        self.storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_address_is_suppressed_after_optout(self) -> None:
        self.storage.suppress(email="Ada@Example.com", reason="unsubscribe")
        self.assertIsNotNone(self.storage.is_suppressed("ada@example.com"))

    def test_lookup_is_case_insensitive(self) -> None:
        self.storage.suppress(email="ada@example.com", reason="unsubscribe")
        self.assertIsNotNone(self.storage.is_suppressed("ADA@EXAMPLE.COM"))

    def test_optout_covers_the_whole_domain(self) -> None:
        """One person opting out speaks for the company, not just the inbox."""
        self.storage.suppress(domain="example.com", reason="complaint")
        self.assertIsNotNone(self.storage.is_suppressed("someone-else@example.com"))

    def test_domain_block_cannot_be_bypassed_by_passing_only_the_address(self) -> None:
        self.storage.suppress(domain="example.com", reason="manual")
        self.assertIsNotNone(self.storage.is_suppressed("new@example.com", None))

    def test_unrelated_address_is_not_suppressed(self) -> None:
        self.storage.suppress(email="ada@example.com", reason="unsubscribe")
        self.assertIsNone(self.storage.is_suppressed("ada@other.com"))

    def test_suppressing_twice_does_not_duplicate(self) -> None:
        a = self.storage.suppress(email="ada@example.com", reason="unsubscribe")
        b = self.storage.suppress(email="ada@example.com", reason="bounce")
        self.assertEqual(a, b)
        self.assertEqual(len(self.storage.list_suppressions()), 1)

    def test_removal_keeps_the_record(self) -> None:
        """`active = 0`, never DELETE: the evidence that a request was honoured
        is the only thing that can be shown to a regulator later."""
        self.storage.suppress(email="ada@example.com", reason="unsubscribe")
        self.storage.unsuppress(email="ada@example.com")
        self.assertIsNone(self.storage.is_suppressed("ada@example.com"))
        self.assertEqual(len(self.storage.list_suppressions(active_only=False)), 1)

    def test_unknown_reason_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.suppress(email="ada@example.com", reason="because")

    def test_empty_target_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.suppress(reason="manual")


class FooterIdentityTests(unittest.TestCase):
    """A real send must carry the controller's registered identity."""

    def _sender(self, **env):
        for k, v in env.items():
            os.environ[k] = v
        return _reload("sender")

    def test_real_send_without_postal_address_refuses(self) -> None:
        sender = self._sender(DRY_RUN="false", SENDER_POSTAL_ADDRESS="")
        with self.assertRaises(sender.IdentityMissing):
            sender._with_footer("hello")

    def test_footer_carries_address_optout_and_policy(self) -> None:
        sender = self._sender(
            DRY_RUN="false",
            SENDER_POSTAL_ADDRESS="Test s.r.o., Hlavna 1, Bratislava, IČO 12345678",
        )
        body = sender._with_footer("hello")
        self.assertIn("IČO 12345678", body)
        self.assertIn("unsubscribe", body.lower())
        self.assertIn("/privacy", body)

    def test_footer_says_the_message_is_a_solicitation(self) -> None:
        """CAN-SPAM wants the message to admit what it is, not just who sent it.

        Address and opt-out were already covered; identification was not, and
        it is a separate required element for every recipient who has not
        consented in advance — which is all of them.
        """
        sender = self._sender(
            DRY_RUN="false",
            SENDER_POSTAL_ADDRESS="Test s.r.o., Hlavna 1, Bratislava, IČO 12345678",
        )
        body = sender._with_footer("hello")
        self.assertIn(sender.AD_IDENTIFICATION, body)
        self.assertLess(
            body.index(sender.AD_IDENTIFICATION), body.index("Don't want these?"),
            "identification belongs above the opt-out, not after it",
        )

    def test_dry_run_still_works_before_the_identity_is_decided(self) -> None:
        sender = self._sender(DRY_RUN="true", SENDER_POSTAL_ADDRESS="")
        self.assertIn("unsubscribe", sender._with_footer("hello").lower())

    def test_optout_header_is_set(self) -> None:
        sender = self._sender(DRY_RUN="true", UNSUBSCRIBE_EMAIL="hello@smuz.io")
        self.assertIn("mailto:hello@smuz.io", sender._unsubscribe_headers()["List-Unsubscribe"])


if __name__ == "__main__":
    unittest.main()
