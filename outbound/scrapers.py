"""Lead discovery from public sources.

YC: yc-oss publishes a community JSON mirror of the public YC company directory
at https://yc-oss.github.io/api/ . Reliable, no auth, refreshed often.

ProductHunt: tries the public RSS feed first (no auth). If a PRODUCTHUNT_TOKEN
is set, falls back to the GraphQL v2 API for richer fields.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import settings


YC_BATCH_URL = "https://yc-oss.github.io/api/batches/{batch}.json"
YC_ALL_URL = "https://yc-oss.github.io/api/companies/all.json"
PH_RSS_URL = "https://www.producthunt.com/feed"
PH_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host or None
    except Exception:
        return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _get_json(url: str, **kwargs: Any) -> Any:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        r = client.get(url, **kwargs)
        r.raise_for_status()
        return r.json()


def fetch_yc_companies(batch: str | None = None, limit: int = 25) -> list[dict]:
    """Return a list of YC companies. Filter to small/early-stage by default.

    `batch` is like 'W25', 'S24'. If omitted, returns from the all-companies dump
    filtered to the most recent batches.
    """
    if batch:
        url = YC_BATCH_URL.format(batch=batch.lower())
    else:
        url = YC_ALL_URL

    data = _get_json(url)
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for c in data:
        team_size = c.get("team_size") or 0
        # Pre-seed / seed proxy: small teams, recent batches. Tune as needed.
        if team_size and team_size > 25:
            continue
        out.append({
            "source": "yc",
            "source_ref": c.get("slug") or c.get("name", "").lower().replace(" ", "-"),
            "company_name": c.get("name", "").strip(),
            "company_url": c.get("website") or None,
            "company_domain": _domain_of(c.get("website")),
            "one_liner": (c.get("one_liner") or "").strip() or None,
            "founder_name": _first_founder(c),
            "batch": c.get("batch"),
            "industry": c.get("industry"),
        })
        if len(out) >= limit:
            break
    return out


def _first_founder(company: dict) -> str | None:
    founders = company.get("founders") or []
    if founders and isinstance(founders, list):
        first = founders[0]
        if isinstance(first, dict):
            return (first.get("full_name") or first.get("name") or "").strip() or None
        if isinstance(first, str):
            return first.strip() or None
    return None


# --- ProductHunt --------------------------------------------------------------

def fetch_producthunt_recent(limit: int = 25) -> list[dict]:
    """Recent PH launches via RSS (no auth needed). Returns leads with
    company_name + url. Founder name is rarely in the feed — enrichment will
    backfill from the product page when possible.
    """
    if settings.producthunt_token:
        try:
            return _ph_graphql(limit=limit)
        except Exception:
            pass  # fall through to RSS

    feed = feedparser.parse(PH_RSS_URL)
    out: list[dict] = []
    for entry in feed.entries[:limit]:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        # PH titles look like "Acme — short tagline"; split if present.
        name, _, tagline = title.partition(" — ")
        link = entry.get("link")
        # entry.link points to the PH post page. The "external" homepage is in
        # the description as an <a href>; pull the first http url that isn't PH.
        ext_url = _extract_external_url(entry.get("summary", "")) or link
        out.append({
            "source": "producthunt",
            "source_ref": entry.get("id") or link,
            "company_name": name.strip(),
            "company_url": ext_url,
            "company_domain": _domain_of(ext_url),
            "one_liner": tagline.strip() or None,
            "founder_name": None,
            "ph_post_url": link,
        })
    return out


_EXT_URL_RE = re.compile(r'href="(https?://[^"]+)"')


def _extract_external_url(html: str) -> str | None:
    for m in _EXT_URL_RE.finditer(html or ""):
        url = m.group(1)
        if "producthunt.com" not in url:
            return url
    return None


# --- Apollo.io ---------------------------------------------------------------

# Apollo returns this literal local-part when an email exists but is locked
# behind a credit unlock. We treat it as "no email" and let pattern-guessing
# take over — keeping the free tier free.
APOLLO_LOCKED_LOCALPART = "email_not_unlocked"

DEFAULT_APOLLO_TITLES = ["Founder", "Co-Founder", "Co Founder", "CEO", "Founder & CEO"]
DEFAULT_APOLLO_EMPLOYEE_RANGES = ["1,10", "11,20"]
DEFAULT_APOLLO_FUNDING_STAGES = ["0", "10"]  # 0=Seed, 10=Pre-Seed in Apollo's stage codes


def fetch_apollo_founders(
    limit: int = 25,
    titles: list[str] | None = None,
    employee_ranges: list[str] | None = None,
    funding_stages: list[str] | None = None,
    keywords: str | None = None,
) -> list[dict]:
    """Search Apollo for pre-seed/seed founders.

    Free-tier-safe: this only calls the search endpoint (no per-call cost) and
    never asks Apollo to unlock anything. We pass through verified emails when
    Apollo gives them for free; locked ones become None and the rest of the
    pipeline falls back to pattern-guessing in `enrichment.find_email`.
    """
    if not settings.apollo_api_key:
        return []

    body: dict[str, Any] = {
        "person_titles": titles or DEFAULT_APOLLO_TITLES,
        "organization_num_employees_ranges": employee_ranges or DEFAULT_APOLLO_EMPLOYEE_RANGES,
        "organization_latest_funding_stage_cd": funding_stages or DEFAULT_APOLLO_FUNDING_STAGES,
        "per_page": min(max(limit, 1), 25),
        "page": 1,
    }
    if keywords:
        body["q_keywords"] = keywords

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": settings.apollo_api_key,
    }

    with httpx.Client(timeout=30.0) as client:
        r = client.post(APOLLO_SEARCH_URL, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()

    out: list[dict] = []
    for p in (data.get("people") or [])[:limit]:
        org = p.get("organization") or {}
        website = org.get("website_url") or org.get("primary_domain")
        email = p.get("email")
        if email and email.startswith(f"{APOLLO_LOCKED_LOCALPART}@"):
            email = None
        elif email and p.get("email_status") not in {"verified", "likely to engage"}:
            # Treat unverified/guessed Apollo emails as untrustworthy and re-derive.
            email = None
        full_name = (p.get("name")
                     or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()) or None
        out.append({
            "source": "apollo",
            "source_ref": p.get("id") or f"{full_name}@{_domain_of(website)}",
            "company_name": (org.get("name") or "").strip(),
            "company_url": website,
            "company_domain": _domain_of(website),
            "founder_name": full_name,
            "founder_email": email,
            "one_liner": (org.get("short_description") or org.get("seo_description") or None),
            "apollo_person_id": p.get("id"),
            "apollo_email_status": p.get("email_status"),
        })
    return out


def _ph_graphql(limit: int) -> list[dict]:
    query = """
    query RecentPosts($first: Int!) {
      posts(order: NEWEST, first: $first) {
        edges { node {
          id slug name tagline website url
          user { name }
        } }
      }
    }
    """
    headers = {"Authorization": f"Bearer {settings.producthunt_token}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            PH_GRAPHQL_URL,
            json={"query": query, "variables": {"first": limit}},
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
    edges = data.get("data", {}).get("posts", {}).get("edges", [])
    out: list[dict] = []
    for e in edges:
        n = e["node"]
        out.append({
            "source": "producthunt",
            "source_ref": n["id"],
            "company_name": n["name"],
            "company_url": n.get("website"),
            "company_domain": _domain_of(n.get("website")),
            "one_liner": n.get("tagline"),
            "founder_name": (n.get("user") or {}).get("name"),
            "ph_post_url": n.get("url"),
        })
    return out
