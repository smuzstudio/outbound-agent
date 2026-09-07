"""Tests for reading the researched cohort out of targets.csv.

Two of these guard bugs that would be invisible in production rather than
loud, which is the only reason they are worth the lines:

  - `test_ats_url_never_becomes_a_domain`. Most evidence URLs point at a job
    board, not the company. If the loader derived a domain from them, dozens of
    unrelated companies would share `job-boards.greenhouse.io` — and because
    `already_contacted()` dedupes by domain, the first one contacted would
    silently suppress every other. Nothing would error; the cohort would just
    quietly stop producing leads.

  - `test_row_without_country_is_held_back`. An unknown country is a refusal at
    send time. Emitting the lead anyway produces a row that can never be sent
    and never explains itself.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from outbound import cohort  # noqa: E402

HEADER = "tier,company,hq,evidence_url,evidence,score,country,website\n"


def _csv(*rows: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(HEADER)
    for r in rows:
        f.write(r + "\n")
    f.close()
    return f.name


class CohortTests(unittest.TestCase):
    def test_usable_row_becomes_a_lead(self) -> None:
        p = _csv("A,Acme,\"Austin, TX, US\",https://acme.com/about,evidence,9.0,US,acme.com")
        leads = cohort.load_targets(p, tiers=("A",))
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["source"], "targets")
        self.assertEqual(lead["country"], "US")
        self.assertEqual(lead["company_domain"], "acme.com")
        self.assertEqual(lead["company_url"], "https://acme.com")
        # The sweep qualified companies, not people.
        self.assertIsNone(lead["founder_name"])
        self.assertIsNone(lead["founder_email"])

    def test_row_without_country_is_held_back(self) -> None:
        p = _csv("A,Acme,unknown,https://acme.com,evidence,9.0,,acme.com")
        self.assertEqual(cohort.load_targets(p, tiers=("A",)), [])
        self.assertEqual(cohort.summarise(p, tiers=("A",))["missing_country"], 1)

    def test_row_without_website_is_held_back(self) -> None:
        p = _csv("A,Acme,\"Austin, TX, US\",https://job-boards.greenhouse.io/acme/1,e,9.0,US,")
        self.assertEqual(cohort.load_targets(p, tiers=("A",)), [])
        self.assertEqual(cohort.summarise(p, tiers=("A",))["missing_website"], 1)

    def test_ats_url_never_becomes_a_domain(self) -> None:
        """Two companies whose only evidence is the same job board.

        Neither may be emitted, because both would carry the board's domain and
        the second would be deduped away against the first.
        """
        p = _csv(
            "A,Acme,\"Austin, TX, US\",https://job-boards.greenhouse.io/acme/1,e,9.0,US,",
            "A,Beta,\"Boston, MA, US\",https://job-boards.greenhouse.io/beta/2,e,9.0,US,",
        )
        leads = cohort.load_targets(p, tiers=("A",))
        self.assertEqual(leads, [])
        domains = {l["company_domain"] for l in leads}
        self.assertNotIn("job-boards.greenhouse.io", domains)

    def test_tier_filter_excludes_other_tiers(self) -> None:
        p = _csv(
            "A,Acme,\"Austin, TX, US\",https://acme.com,e,9.0,US,acme.com",
            "D,Zeta,\"Austin, TX, US\",https://zeta.com,e,2.0,US,zeta.com",
        )
        names = [l["company_name"] for l in cohort.load_targets(p, tiers=("A",))]
        self.assertEqual(names, ["Acme"])

    def test_source_ref_is_stable_across_reordering(self) -> None:
        """Re-exporting the CSV must not make every row look new.

        source_ref keys the UNIQUE(source, source_ref) constraint. If it were
        positional, one re-sort would re-insert the entire cohort as fresh
        leads and the dedupe would have nothing to match on.
        """
        a = _csv("A,Acme,\"Austin, TX, US\",https://acme.com,e,9.0,US,acme.com",
                 "A,Beta,\"Boston, MA, US\",https://beta.com,e,9.0,US,beta.com")
        b = _csv("A,Beta,\"Boston, MA, US\",https://beta.com,e,9.0,US,beta.com",
                 "A,Acme,\"Austin, TX, US\",https://acme.com,e,9.0,US,acme.com")
        refs = lambda p: {l["company_name"]: l["source_ref"]
                          for l in cohort.load_targets(p, tiers=("A",))}
        self.assertEqual(refs(a), refs(b))

    def test_missing_csv_is_reported_not_raised(self) -> None:
        s = cohort.summarise("/nonexistent/targets.csv", tiers=("A",))
        self.assertFalse(s["csv_present"])
        self.assertEqual(cohort.load_targets("/nonexistent/targets.csv"), [])


if __name__ == "__main__":
    unittest.main()
