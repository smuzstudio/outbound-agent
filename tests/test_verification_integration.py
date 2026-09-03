"""The two repos must agree on the schema.

`runproof`'s own tests inline a copy of this schema so the tool stays
independently testable — which means they would keep passing if the real
schema drifted underneath them. The assertions would then be checking a
table shape that no longer exists, and would report ERROR in production
while the suite stayed green.

This test closes that gap: it builds a database with the *real* storage
layer and runs the *real* profile against it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNPROOF = ROOT.parent / "runproof"

runproof_available = RUNPROOF.exists()
if runproof_available:
    sys.path.insert(0, str(RUNPROOF))


@unittest.skipUnless(runproof_available, "runproof checkout not found alongside this repo")
class VerificationIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "outbound.db"
        os.environ["DB_PATH"] = str(self.db)
        for mod in [m for m in list(sys.modules) if m.startswith("outbound")]:
            del sys.modules[mod]
        sys.path.insert(0, str(ROOT))
        from outbound import storage
        self.storage = storage

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _healthy_database(self) -> None:
        st = self.storage
        st.init_db()
        run_id = st.start_run(source="yc", requested_n=3, dry_run=False, daily_cap=15)

        # Place the run inside the schedule window the profile expects.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                    (f"{today}T09:05:00+00:00", run_id))
        con.commit()
        con.close()

        for i, domain in enumerate(["acme.com", "beta.io", "ceres.dev"]):
            lead = st.upsert_lead(source="yc", source_ref=f"s{i}",
                                  company_name=f"Co{i}", company_domain=domain,
                                  founder_email=f"f{i}@{domain}")
            if i < 2:
                sid = st.reserve_send(lead_id=lead, to_email=f"f{i}@{domain}",
                                      subject="s", body="b", provider="pending",
                                      daily_cap=15)
                st.confirm_send(sid, provider="resend", provider_message_id=f"m{i}")
                st.update_lead(lead, status="sent")
            else:
                st.update_lead(lead, status="skipped", skip_reason="late_stage")
        st.finish_run(run_id, status="ok", cost_usd=0.42)

    def _check(self):
        from runproof import config as config_mod
        from runproof.cli import _run_profile

        profile = config_mod.load(RUNPROOF / "runproof" / "profiles" / "outbound.toml")
        profile.database = self.db
        return _run_profile(profile, datetime.now(timezone.utc))

    def test_real_schema_satisfies_every_assertion(self):
        self._healthy_database()
        report = self._check()
        self.assertTrue(
            report.passed,
            "\n".join(f"{r.assertion.id}: {r.status.value} — {r.detail}"
                      for r in report.failures),
        )

    def test_no_assertion_errors_against_the_real_schema(self):
        """The drift detector.

        An ERROR means a table or column the profile names is not there. That
        is the failure this file exists to catch, and it is distinct from an
        assertion legitimately failing.
        """
        from runproof.model import Status

        self._healthy_database()
        report = self._check()
        errored = [r for r in report.results if r.status is Status.ERROR]
        self.assertEqual(
            errored, [],
            "profile references schema this repo no longer has:\n"
            + "\n".join(f"{r.assertion.id}: {r.detail}" for r in errored),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
