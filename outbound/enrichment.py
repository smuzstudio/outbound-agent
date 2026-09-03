"""Lead enrichment: read a company's site, find a likely founder email.

Two responsibilities, kept in one file so the agent has one place to look:

1. `research_company(url)` -> trimmed text + metadata. Used by the LLM to
   write a personalized opener. We pull the homepage + an /about-style page
   if linked, strip to readable text, and truncate.

2. `find_email(name, domain)` -> a best-guess email. Optionally validated via
   Hunter.io if HUNTER_API_KEY is set; otherwise pattern-guessing only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import settings


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

ABOUT_PATHS = ["/about", "/about-us", "/company", "/team", "/our-team"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class CompanyResearch:
    url: str
    title: str | None
    headline: str | None
    body_excerpt: str
    emails_found: list[str]
    socials: list[str]


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def _fetch(url: str) -> str:
    with httpx.Client(timeout=20.0, follow_redirects=True, headers=HEADERS) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def research_company(url: str) -> dict:
    """Fetch a company's homepage + an about page if found. Return trimmed,
    LLM-friendly research notes.
    """
    pages_html: list[tuple[str, str]] = []
    try:
        pages_html.append((url, _fetch(url)))
    except Exception as e:
        return asdict(CompanyResearch(
            url=url, title=None, headline=None,
            body_excerpt=f"(failed to fetch: {e})", emails_found=[], socials=[],
        ))

    # Try to grab an about/team page if linked from homepage.
    soup = BeautifulSoup(pages_html[0][1], "lxml")
    about_link = None
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if any(p in href for p in ABOUT_PATHS):
            about_link = urljoin(url, a["href"])
            break
    if about_link and about_link != url:
        try:
            pages_html.append((about_link, _fetch(about_link)))
        except Exception:
            pass

    title = (soup.title.string.strip() if soup.title and soup.title.string else None)
    headline = None
    h1 = soup.find("h1")
    if h1:
        headline = h1.get_text(" ", strip=True)[:200]

    body_parts: list[str] = []
    emails: set[str] = set()
    socials: set[str] = set()
    for page_url, html in pages_html:
        s = BeautifulSoup(html, "lxml")
        for tag in s(["script", "style", "noscript", "svg", "img", "header", "footer", "nav"]):
            tag.decompose()
        text = s.get_text(" ", strip=True)
        body_parts.append(f"--- {page_url} ---\n{text[:2500]}")
        emails.update(EMAIL_RE.findall(html))
        for a in s.find_all("a", href=True):
            href = a["href"]
            if any(d in href for d in ("twitter.com/", "x.com/", "linkedin.com/in/", "linkedin.com/company/")):
                socials.add(href)

    body_excerpt = "\n\n".join(body_parts)[:5000]
    # Filter generic / vendor emails out of the founder-email candidates.
    emails_clean = sorted({
        e for e in emails
        if not any(b in e.lower() for b in ("sentry", "wixpress", "godaddy", "example.com"))
    })

    return asdict(CompanyResearch(
        url=url, title=title, headline=headline,
        body_excerpt=body_excerpt,
        emails_found=emails_clean[:10],
        socials=sorted(socials)[:10],
    ))


# --- Email finder -------------------------------------------------------------

COMMON_PATTERNS = [
    "{first}@{domain}",
    "{first}.{last}@{domain}",
    "{f}{last}@{domain}",
    "{first}{last}@{domain}",
    "{first}_{last}@{domain}",
    "{f}.{last}@{domain}",
]


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", full_name.strip().lower()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _hunter_find(full_name: str, domain: str) -> str | None:
    if not settings.hunter_api_key:
        return None
    first, last = _split_name(full_name)
    params = {"domain": domain, "first_name": first, "last_name": last,
              "api_key": settings.hunter_api_key}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get("https://api.hunter.io/v2/email-finder", params=params)
            r.raise_for_status()
            data = r.json().get("data") or {}
            email = data.get("email")
            score = data.get("score") or 0
            if email and score >= 50:
                return email
    except Exception:
        return None
    return None


def find_email(full_name: str | None, domain: str | None,
               also_consider: list[str] | None = None) -> dict:
    """Return a best-guess email + how we got there.

    Order of preference:
      1. An email Hunter.io confirms (if key present).
      2. A founder-looking address already on the company site.
      3. Pattern-guessed `{first}@{domain}`.
    """
    also_consider = also_consider or []
    if not domain:
        return {"email": None, "confidence": "none", "candidates": []}

    if full_name:
        hit = _hunter_find(full_name, domain)
        if hit:
            return {"email": hit, "confidence": "hunter", "candidates": [hit]}

    for e in also_consider:
        local = e.split("@")[0].lower()
        if local in {"hello", "info", "contact", "support", "team"}:
            continue
        if domain in e.lower():
            return {"email": e, "confidence": "site", "candidates": also_consider}

    candidates: list[str] = []
    if full_name:
        first, last = _split_name(full_name)
        for pat in COMMON_PATTERNS:
            if "{last}" in pat and not last:
                continue
            candidates.append(pat.format(
                first=first, last=last, f=first[:1] if first else "", domain=domain,
            ))

    if not candidates:
        candidates = [f"hello@{domain}", f"founders@{domain}"]
    return {"email": candidates[0], "confidence": "guess", "candidates": candidates}
