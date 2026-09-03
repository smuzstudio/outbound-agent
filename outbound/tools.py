"""Claude Agent SDK tool wrappers — the agent's hands.

Each @tool returns the MCP content shape expected by the SDK:
    {"content": [{"type": "text", "text": "..."}]}

Keep tool surface small and return JSON-as-text so the model can reason over
fields without us pre-summarizing.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from . import enrichment, scrapers, sender, storage
from .config import DRYRUN_PROVIDER, settings


def _text(payload: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


# --- Discovery ---------------------------------------------------------------

@tool(
    "discover_leads",
    "Discover new early-stage founder leads from a public source and store them. "
    "Returns the newly inserted leads (already-known ones are skipped). "
    "source: 'yc' | 'producthunt' | 'apollo'. For YC you can pass batch like "
    "'W25'. For Apollo you can pass `keywords` to bias the search.",
    {"source": str, "limit": int, "batch": str, "keywords": str},
)
async def discover_leads(args: dict) -> dict:
    source = (args.get("source") or "yc").lower()
    limit = int(args.get("limit") or 10)
    batch = args.get("batch") or None
    keywords = args.get("keywords") or None

    if source == "yc":
        raw = scrapers.fetch_yc_companies(batch=batch, limit=limit * 3)
    elif source == "producthunt":
        raw = scrapers.fetch_producthunt_recent(limit=limit * 3)
    elif source == "apollo":
        if not settings.apollo_api_key:
            return _text({"error": "APOLLO_API_KEY not set in .env"})
        raw = scrapers.fetch_apollo_founders(limit=limit * 2, keywords=keywords)
    else:
        return _text({"error": f"unknown source: {source}"})

    inserted: list[dict] = []
    for lead in raw:
        if not lead.get("company_name"):
            continue
        # Skip if we've ever contacted anyone at this domain.
        if storage.already_contacted(None, lead.get("company_domain")):
            continue
        lead_id = storage.upsert_lead(
            source=lead["source"],
            source_ref=lead["source_ref"],
            company_name=lead["company_name"],
            company_url=lead.get("company_url"),
            company_domain=lead.get("company_domain"),
            founder_name=lead.get("founder_name"),
            founder_email=lead.get("founder_email"),
            one_liner=lead.get("one_liner"),
        )
        if lead_id is not None:
            inserted.append({"lead_id": lead_id, **lead})
        if len(inserted) >= limit:
            break

    return _text({
        "source": source,
        "inserted": len(inserted),
        "leads": inserted,
    })


# --- Research ---------------------------------------------------------------

@tool(
    "research_lead",
    "Fetch the lead's company site, extract a readable excerpt + candidate "
    "emails, and try to resolve a founder email. Updates the lead row and "
    "returns the research notes for the model to read.",
    {"lead_id": int},
)
async def research_lead(args: dict) -> dict:
    lead_id = int(args["lead_id"])
    lead = storage.get_lead(lead_id)
    if not lead:
        return _text({"error": f"no such lead: {lead_id}"})

    url = lead.get("company_url")
    if not url:
        storage.update_lead(lead_id, status="skipped", skip_reason="no_url")
        return _text({"lead_id": lead_id, "status": "skipped", "reason": "no_url"})

    notes = enrichment.research_company(url)
    domain = lead.get("company_domain")

    # If discovery already gave us a verified email (e.g. Apollo), keep it —
    # don't overwrite with a pattern guess.
    if lead.get("founder_email"):
        found = {"email": lead["founder_email"], "confidence": "source",
                 "candidates": [lead["founder_email"]]}
        storage.update_lead(lead_id, research_notes=notes, status="researched")
    else:
        found = enrichment.find_email(
            full_name=lead.get("founder_name"),
            domain=domain,
            also_consider=notes.get("emails_found", []),
        )
        storage.update_lead(
            lead_id,
            founder_email=found.get("email"),
            research_notes=notes,
            status="researched",
        )

    return _text({
        "lead_id": lead_id,
        "company_name": lead["company_name"],
        "company_url": url,
        "founder_name": lead.get("founder_name"),
        "email": found,
        "research": notes,
    })


# --- Send / skip ------------------------------------------------------------

@tool(
    "send_email",
    "Send a cold email to the lead. The agent is responsible for the subject "
    "and body (a footer + unsubscribe line is appended automatically). "
    "Enforces the daily send cap and per-domain dedupe.",
    {"lead_id": int, "subject": str, "body": str},
)
async def send_email_tool(args: dict) -> dict:
    lead_id = int(args["lead_id"])
    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()

    if not subject or not body:
        return _text({"error": "subject and body are both required"})

    lead = storage.get_lead(lead_id)
    if not lead:
        return _text({"error": f"no such lead: {lead_id}"})
    to_email = lead.get("founder_email")
    if not to_email:
        return _text({"error": "lead has no founder_email — call research_lead first or skip"})

    if storage.already_contacted(to_email, lead.get("company_domain")):
        storage.update_lead(lead_id, status="skipped", skip_reason="already_contacted")
        return _text({"lead_id": lead_id, "status": "skipped", "reason": "already_contacted"})

    # Claim the slot before sending, not after. Two things follow from that
    # order: the cap becomes atomic (the count and the claim are one
    # transaction), and a crash mid-send leaves a row behind rather than a
    # silent gap. Both failures then point the same way — toward not
    # emailing someone twice.
    reserve_provider = DRYRUN_PROVIDER if settings.dry_run else "pending"
    try:
        send_id = storage.reserve_send(
            lead_id=lead_id, to_email=to_email, subject=subject, body=body,
            provider=reserve_provider, daily_cap=settings.daily_send_cap,
        )
    except storage.CapReached as exc:
        return _text({"error": str(exc), "lead_id": lead_id,
                      "note": "stop sending for today"})

    try:
        result = sender.send_email(to_email=to_email, subject=subject, body=body)
    except sender.TransportRejected as exc:
        # The transport refused it outright; nothing was queued. Free the slot
        # so the cap isn't spent on a message that never existed.
        storage.release_send(send_id, error=str(exc))
        storage.update_lead(lead_id, status="skipped", skip_reason="send_rejected")
        return _text({"lead_id": lead_id, "status": "send_rejected",
                      "error": str(exc),
                      "note": "slot released — skip this lead and continue"})
    except sender.TransportAmbiguous as exc:
        # Might have gone out. The reservation stands and keeps counting.
        storage.record_send_error(send_id, error=str(exc))
        storage.update_lead(lead_id, status="send_failed")
        return _text({"lead_id": lead_id, "status": "send_failed",
                      "error": str(exc),
                      "note": "outcome unknown, treated as contacted — "
                              "do not retry this lead; continue with the next"})

    storage.confirm_send(send_id, provider=result["provider"],
                         provider_message_id=result.get("message_id"))
    storage.update_lead(lead_id, status="sent")
    return _text({
        "lead_id": lead_id, "to": to_email, "subject": subject,
        "provider": result["provider"], "dry_run": result.get("dry_run", False),
        "sent_today": storage.sends_today(), "daily_cap": settings.daily_send_cap,
    })


@tool(
    "skip_lead",
    "Mark a lead as skipped with a short reason. Use when not a fit, no email "
    "found, or any other disqualifier. Don't email them after skipping.",
    {"lead_id": int, "reason": str},
)
async def skip_lead(args: dict) -> dict:
    lead_id = int(args["lead_id"])
    reason = (args.get("reason") or "").strip() or "unspecified"
    storage.update_lead(lead_id, status="skipped", skip_reason=reason)
    return _text({"lead_id": lead_id, "status": "skipped", "reason": reason})


# --- Inspection -------------------------------------------------------------

@tool(
    "list_leads",
    "List leads in the database, optionally filtered by status "
    "('new' | 'researched' | 'sent' | 'skipped').",
    {"status": str, "limit": int},
)
async def list_leads(args: dict) -> dict:
    status = args.get("status") or None
    limit = int(args.get("limit") or 25)
    return _text({"leads": storage.list_leads(status=status, limit=limit)})


ALL_TOOLS = [discover_leads, research_lead, send_email_tool, skip_lead, list_leads]
ALLOWED_TOOL_NAMES = [
    "mcp__outbound__discover_leads",
    "mcp__outbound__research_lead",
    "mcp__outbound__send_email",
    "mcp__outbound__skip_lead",
    "mcp__outbound__list_leads",
]
