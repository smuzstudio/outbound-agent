"""Tests for the send lifecycle, the daily cap, and the run lock.

The cap test spawns real processes. F4 was a race between a read and a write
in separate transactions; a single-process test cannot observe a race, so a
single-process test would prove nothing about the thing that was broken.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _worker(db_path: str, cap: int, index: int, results) -> None:
    """Run in a fresh process: import the module, try to claim one slot."""
    os.environ["DB_PATH"] = db_path
    sys.path.insert(0, str(ROOT))
    from outbound import storage  # imported here so it reads this env

    try:
        storage.reserve_send(
            lead_id=1, to_email=f"r{index}@example.com", subject="s", body="b",
            provider="pending", daily_cap=cap,
        )
        results.append("reserved")
    except storage.CapReached:
        results.append("capped")


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "outbound.db"
        os.environ["DB_PATH"] = str(self.db)
        # Reimport under this DB_PATH; Settings is frozen at import time.
        for mod in [m for m in list(sys.modules) if m.startswith("outbound")]:
            del sys.modules[mod]
        sys.path.insert(0, str(ROOT))
        from outbound import storage
        self.storage = storage
        storage.init_db()
        self.run_id = storage.start_run(source="yc", requested_n=5, dry_run=False,
                                        daily_cap=5)
        self.lead = storage.upsert_lead(source="yc", source_ref="a",
                                        company_name="Acme",
                                        company_domain="acme.com",
                                        founder_email="f@acme.com")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reserve(self, email="f@acme.com", provider="pending", cap=5):
        return self.storage.reserve_send(lead_id=self.lead, to_email=email,
                                         subject="s", body="b", provider=provider,
                                         daily_cap=cap)

    # --- lifecycle ----------------------------------------------------------

    def test_confirmed_send_counts_as_contact(self):
        sid = self.reserve()
        self.storage.confirm_send(sid, provider="resend", provider_message_id="m1")
        self.assertTrue(self.storage.already_contacted("f@acme.com", None))
        self.assertTrue(self.storage.already_contacted(None, "acme.com"))
        self.assertEqual(self.storage.sends_today(), 1)

    def test_released_send_does_not_count(self):
        # Positive evidence of rejection: the lead must stay reachable and
        # the cap must not be spent on a message that never existed.
        sid = self.reserve()
        self.storage.release_send(sid, error="resend 422: invalid address")
        self.assertFalse(self.storage.already_contacted("f@acme.com", None))
        self.assertEqual(self.storage.sends_today(), 0)

    def test_unconfirmed_reservation_still_counts(self):
        # The crash window. We do not know whether it went out, so we assume
        # it did: a duplicate email is visible to the recipient, a missed one
        # is not.
        self.reserve()
        self.assertTrue(self.storage.already_contacted("f@acme.com", None))
        self.assertEqual(self.storage.sends_today(), 1)

    def test_dry_run_never_consumes_the_cap(self):
        for i in range(10):
            self.reserve(email=f"d{i}@acme.com",
                         provider=self.storage.DRYRUN_PROVIDER)
        self.assertEqual(self.storage.sends_today(), 0)
        self.assertFalse(self.storage.already_contacted("d0@acme.com", None))

    def test_cap_blocks_the_next_reservation(self):
        for i in range(5):
            self.reserve(email=f"c{i}@acme.com")
        with self.assertRaises(self.storage.CapReached):
            self.reserve(email="over@acme.com")

    # --- the actual finding -------------------------------------------------

    def test_concurrent_processes_cannot_oversend(self):
        """20 processes, cap of 5. Exactly 5 may claim a slot."""
        cap = 5
        with mp.Manager() as manager:
            results = manager.list()
            procs = [
                mp.Process(target=_worker, args=(str(self.db), cap, i, results))
                for i in range(20)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
            outcomes = list(results)

        self.assertEqual(len(outcomes), 20, "a worker died")
        self.assertEqual(outcomes.count("reserved"), cap,
                         f"cap breached or under-filled: {outcomes.count('reserved')}")
        self.assertEqual(self.storage.sends_today(), cap)


class RunLockTest(unittest.TestCase):
    def test_second_instance_does_not_acquire(self):
        sys.path.insert(0, str(ROOT))
        from outbound.main import _single_instance

        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "outbound.lock"
            with _single_instance(lock) as first:
                self.assertTrue(first)
                # A second acquisition from another *process* is what cron
                # double-firing looks like; flock is per-open-file-description,
                # so a fresh open in this process models it faithfully.
                with _single_instance(lock) as second:
                    self.assertFalse(second)
            # Released — the next run can take it.
            with _single_instance(lock) as third:
                self.assertTrue(third)


if __name__ == "__main__":
    unittest.main(verbosity=2)
