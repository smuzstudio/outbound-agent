"""The researched target cohort, read from `targets.csv`.

The other discovery sources go and find companies. This one does not: the 220
rows were qualified by hand in a research sweep, and this module only reads
them. That difference matters for two fields the scrapers get for free and we
have to be careful about here.

COUNTRY drives the jurisdiction gate, and an unknown country is a refusal
(jurisdiction.py). The sweep recorded HQ as free text — "Berlin, Germany",
but also "US (HQ city unconfirmed)" and plain "unknown" — so `country` is a
derived ISO-3166 alpha-2 column added to the CSV, deliberately left BLANK
wherever the text did not settle it. Blank reaches `may_send()` as None and is
refused. Guessing here would put mail into a consent-required jurisdiction and
the mistake would be invisible.

WEBSITE is not the evidence URL. `evidence_url` points at whatever proved the
company runs unattended work, and for most rows that is an ATS posting —
41 rows on `job-boards.greenhouse.io`, 36 on `jobs.ashbyhq.com`. Deriving the
domain from it would give dozens of companies the *same* domain, and
`already_contacted()` dedupes by domain: the first greenhouse company would
silently suppress every other one. So `website` is its own column, populated
only when the evidence URL is the company's own site, and rows without one are
held back rather than emitted with a guess.
"""
from __future__ import annotations

import csv
import io
import os
import re
from typing import Iterable

TIERS_DEFAULT = ("A",)


def path() -> str:
    return os.getenv("TARGETS_CSV", "./targets.csv")


def _slug(name: str) -> str:
    """Stable per-company key for the UNIQUE(source, source_ref) constraint.

    Derived from the company name rather than the row index, so re-sorting or
    re-exporting the CSV cannot make every row look new and re-insert the
    whole cohort.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "unknown"


def _rows(csv_path: str | None = None) -> list[dict]:
    p = csv_path or path()
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tier_of(row: dict) -> str:
    return (row.get("tier") or "").strip().upper()


def summarise(csv_path: str | None = None,
              tiers: Iterable[str] = TIERS_DEFAULT) -> dict:
    """Counts, so a caller can see what the cohort cannot yet do.

    Held-back rows are reported, never silently dropped: "12 of 16 usable" is
    actionable and "12 leads" is not.
    """
    want = {t.strip().upper() for t in tiers}
    rows = _rows(csv_path)
    pool = [r for r in rows if _tier_of(r) in want]
    no_country = [r["company"] for r in pool if not (r.get("country") or "").strip()]
    no_site = [r["company"] for r in pool if not (r.get("website") or "").strip()]
    usable = [r for r in pool
              if (r.get("country") or "").strip() and (r.get("website") or "").strip()]
    return {
        "csv_path": csv_path or path(),
        "csv_present": bool(rows),
        "tiers": sorted(want),
        "in_tier": len(pool),
        "usable": len(usable),
        "missing_country": len(no_country),
        "missing_website": len(no_site),
        "missing_country_companies": sorted(no_country)[:25],
        "missing_website_companies": sorted(no_site)[:25],
    }


def load_targets(csv_path: str | None = None,
                 tiers: Iterable[str] = TIERS_DEFAULT,
                 limit: int | None = None) -> list[dict]:
    """Cohort rows as lead dicts, in the shape the scrapers return.

    Only rows carrying BOTH a country and a website are returned. A row missing
    either cannot be lawfully sent or cannot be researched, and inserting it
    would create a lead row that is permanently stuck at `new` — a queue that
    never drains and never says why. `summarise()` reports what was held back.
    """
    want = {t.strip().upper() for t in tiers}
    out: list[dict] = []
    for r in _rows(csv_path):
        if _tier_of(r) not in want:
            continue
        country = (r.get("country") or "").strip().upper()
        site = (r.get("website") or "").strip().lower()
        if not country or not site:
            continue
        name = (r.get("company") or "").strip()
        if not name:
            continue
        out.append({
            "source": "targets",
            "source_ref": _slug(name),
            "company_name": name,
            "company_url": f"https://{site}",
            "company_domain": site,
            # The sweep qualified companies, not people. Resolving the person
            # is a later step; None here keeps find_email() honest rather than
            # letting it pattern-guess against a name we never had.
            "founder_name": None,
            "founder_email": None,
            "one_liner": (r.get("evidence") or "").strip()[:300] or None,
            "country": country,
            "tier": _tier_of(r),
            "score": (r.get("score") or "").strip(),
        })
        if limit and len(out) >= limit:
            break
    return out
