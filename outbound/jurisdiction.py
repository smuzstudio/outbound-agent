"""Which countries this agent may send unsolicited commercial email to.

GDPR governs the personal data. Whether an unsolicited commercial email may be
sent at all is national law implementing the ePrivacy Directive, and it differs
by the RECIPIENT's country, not ours:

The division that matters is CONSENT vs OPT-OUT, and it is not an EU/non-EU
line — three of the consent regimes below are outside the EU entirely:

  - Poland (PKE, in force Nov 2024) requires prior consent for direct marketing
    to an end user, read broadly enough to cover B2B mail to a named person.
  - Germany and Austria (UWG) require consent too.
  - Canada (CASL) requires express or implied consent BEFORE the first message,
    at up to CAD 10M per violation. There is a conspicuous-publication exemption
    (s.10(9)(b)) that plausibly covers a work address published for the role,
    but it is a per-recipient factual test and nothing here records whether it
    was met. Until something does, Canada is excluded.
  - Australia (Spam Act 2003) is the same shape — express or inferred consent,
    plus a 5-business-day unsubscribe deadline rather than 10.
  - The UK (PECR) exempts corporate subscribers, so cold B2B with a working
    opt-out is workable. Sole traders and partnerships are NOT exempt.
  - The US (CAN-SPAM) is opt-out with a required postal address, and also
    requires the message to identify itself as a solicitation — see
    `sender._with_footer`, which is where that element lives.

So the list below is a legal boundary, not a targeting preference, and it lives
in code rather than in the model's prompt on purpose: a jurisdiction rule that
depends on the model remembering it is not a rule. The model can be talked out
of things. This function cannot.

UNKNOWN COUNTRY IS A REFUSAL. Failing open here would mean the rule protects
only the leads whose country we happened to record, which is the same as no
rule — and the gap would be invisible, because the sends would all look fine.
Recording the country is the discovery step's job.

Reviewed 2026-09-07, when CA and AU were added to the default exclusions: the
earlier list reasoned only about the EU, the UK and the US, so two consent
regimes were being treated as opt-out ones. Excluding is the safe direction and
costs nothing today, because nothing has been sent. This is a working boundary
set by a non-lawyer; the brain records that a lawyer should confirm it, and
re-enabling a country is a legal decision, not a config tweak.
"""
from __future__ import annotations

from .config import settings


class Blocked(str):
    """A refusal carrying its reason, so callers can log why."""


def normalise(country: str | None) -> str | None:
    """Accept 'PL', 'pl', 'Poland ' → 'PL'. Anything unrecognised stays None."""
    if not country:
        return None
    c = country.strip().upper()
    if len(c) == 2 and c.isalpha():
        return c
    return _NAMES.get(c)


# Only the countries that actually appear in the target list plus the obvious
# neighbours. A name that is not here resolves to None, which is a refusal —
# the safe direction.
_NAMES = {
    "POLAND": "PL", "GERMANY": "DE", "DEUTSCHLAND": "DE", "AUSTRIA": "AT",
    "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB",
    "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US",
    "NETHERLANDS": "NL", "THE NETHERLANDS": "NL", "SPAIN": "ES", "FRANCE": "FR",
    "ITALY": "IT", "PORTUGAL": "PT", "IRELAND": "IE", "BELGIUM": "BE",
    "DENMARK": "DK", "SWEDEN": "SE", "NORWAY": "NO", "FINLAND": "FI",
    "ESTONIA": "EE", "LATVIA": "LV", "LITHUANIA": "LT", "CZECHIA": "CZ",
    "CZECH REPUBLIC": "CZ", "SLOVAKIA": "SK", "SWITZERLAND": "CH",
    "CANADA": "CA", "AUSTRALIA": "AU", "ISRAEL": "IL", "UKRAINE": "UA",
}


def excluded_countries() -> set[str]:
    raw = settings.marketing_excluded_countries or ""
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def may_send(country: str | None) -> tuple[bool, str]:
    """(allowed, reason). Reason is machine-readable and safe to store."""
    code = normalise(country)
    if code is None:
        return False, "jurisdiction_unknown"
    if code in excluded_countries():
        return False, f"jurisdiction_excluded:{code}"
    return True, "ok"
