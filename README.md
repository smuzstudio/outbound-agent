# Smuz outbound agent

A Claude-Agent-SDK workflow that finds leads, researches each one, and writes a cold email pitching Smuz — agent reliability: finding the scheduled agents that fail silently while still reporting success.

> **Status, 18 Sep 2026: retired as a channel, kept as the audited subject.**
> Smuz stopped cold outreach of every kind on 18 Sep 2026 — no cold email, no purchased
> contact data, no sending agent. This agent sends nothing and will not be scheduled again;
> the researched cohort it read from (`targets.csv`, never committed) has been deleted.
> The code stays public because it is the subject of [`AUDIT.md`](AUDIT.md): a real audit
> of a real agent, with the ten findings, the fixes, and the assertion set that
> [`runproof`](https://github.com/smuzstudio/runproof) checks it against. Read it as a
> worked example, not as a tool Smuz runs.

> **Use the `targets` source. The other three are still aimed at the previous ICP.**
> `scrapers.py` reads YC, ProductHunt and Apollo founder search, and the YC path discards
> companies over 25 people — the inverse of the current target (50–500 employees, something
> running unattended on a schedule). **Nothing is sent from those three:** since 7 Sep 2026
> `main.py` refuses a live run from `yc`, `producthunt` or `apollo` and exits 2, and `targets`
> is the default when no source is given. They stay usable under `DRY_RUN=true`, because
> refusing to look at them would be deleting them rather than fencing them.
>
> `targets` (added 7 Sep 2026) reads the researched cohort from `targets.csv` and is aimed at
> the current ICP. It emits only rows carrying both a country and a website: as of 7 Sep that
> is **6 of the 16 Tier A rows**, the other 10 held back for want of a website. The count is
> reported on every run rather than left to be discovered — see `cohort.py`.

```
discover_leads  →  research_lead  →  send_email | skip_lead
                    (LLM writes the email itself,
                     no templates)
```

Single sender, single product (Smuz). The ask is a question — "what runs on a schedule that nobody watches?" — not a meeting: there is no booking link and that is deliberate. The model is the orchestrator AND the copywriter; the prompt at `outbound/prompts.py` is where voice and offer live.

## Setup

```bash
cd outbound-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in ANTHROPIC_API_KEY and a transport (RESEND_API_KEY or SMTP_*)
```

`DRY_RUN=true` is on by default in `.env.example` — you'll see exactly what would be sent without touching anyone's inbox. Flip to `false` only after you've reviewed a few dry runs.

## Run

```bash
# Default: 10 leads from the researched cohort (current ICP). Tier A by default.
python -m outbound
python -m outbound targets - 10

# The three below are founder-era: they discover the PREVIOUS ICP, so a live run
# from any of them exits 2. Set DRY_RUN=true to inspect what they return.
python -m outbound yc W25 10
python -m outbound producthunt - 5
python -m outbound apollo - 10        # requires APOLLO_API_KEY; free tier works
```

Each run streams progress: the LLM calls `discover_leads`, then for each lead calls `research_lead`, then either `send_email` (with its own subject + body) or `skip_lead`. You see one line per step.

## Daily cron

The schedule the agent was designed around — 15 emails/day, weekdays only — and the checks
that sit beside it. Kept because the audit's liveness assertions are written against this
window. **Nothing below is installed on any machine as of 18 Sep 2026**; see the status note
at the top.

```cron
# crontab -e
# the agent
0  9 * * 1-5  cd ~/Desktop/Smuz/outbound-agent && /usr/bin/env -S DAILY_SEND_CAP=15 ./.venv/bin/python -m outbound targets - 15 >> ./outbound.log 2>&1
# the check on it, half an hour later — it mails its own failures, so it does
# not share the agent's log file or the agent's fate
30 9 * * 1-5  cd ~/Desktop/Smuz/runproof && ./.venv/bin/python -m runproof check -p runproof/profiles/outbound.toml >> ./runproof.log 2>&1
# and a weekly proof that the alert path itself still works
0  9 * * 1    cd ~/Desktop/Smuz/runproof && ./.venv/bin/python -m runproof alert-test -p runproof/profiles/outbound.toml >> ./runproof.log 2>&1
```

Two independent mechanisms bound this, because the earlier version of this
README claimed a guarantee the code did not provide:

- **A run lock.** `main.py` takes an exclusive `flock` on `outbound.lock`
  before starting. A second process — cron double-firing, or you running it
  by hand mid-run — exits immediately without working. The OS releases the
  lock if the process dies, so a crash cannot wedge tomorrow's run.
- **An atomic cap.** `storage.reserve_send()` counts today's sends and claims
  the slot inside one `BEGIN IMMEDIATE` transaction. Concurrent callers
  serialise instead of both reading `n < cap` and both proceeding.

Neither is trusted on its own: [`runproof`](https://github.com/smuzstudio/runproof) asserts the outcome
from the database afterwards (A5), independently of the in-process check.

## What's in the box

| file | role |
|------|------|
| `outbound/main.py` | CLI entry; spins up the Claude Agent SDK loop. |
| `outbound/prompts.py` | System prompt + voice rules. **Edit this to tune messaging.** |
| `outbound/tools.py` | The five tools exposed to Claude: discover, research, send, skip, list. |
| `outbound/scrapers.py` | YC (via `yc-oss` JSON mirror), ProductHunt (RSS, GraphQL fallback), Apollo.io (free-tier search). Aimed at the **previous** ICP, so `main.py` fences all three to dry runs. |
| `outbound/cohort.py` | The researched cohort from `targets.csv`. Reads only — it never invents a country or a domain, and reports what it held back. |
| `outbound/enrichment.py` | Fetch company site + best-guess founder email (Hunter.io optional). |
| `outbound/sender.py` | Resend (preferred) or Gmail/Workspace SMTP. Auto-appends footer + unsubscribe. Distinguishes definitive rejection from an ambiguous outcome. |
| `outbound/storage.py` | SQLite. Run ledger, send reservations, dedupe per-email AND per-domain, daily-cap accounting. |
| `outbound/config.py` | All settings load from `.env` via this module. |

## Tunable things

- **Voice / what to say** — `outbound/prompts.py`. Word-banned list, opener rules, signoff style.
- **Who qualifies** — same file, "qualify" step. Default skips companies >25 employees or post-Series-A signals.
- **Pattern guesses** — `outbound/enrichment.py:COMMON_PATTERNS`. Reorder by what's worked.
- **Daily cap** — `.env` `DAILY_SEND_CAP`. Also enforced live, not just at startup.
- **Dedupe scope** — `outbound/storage.py:already_contacted` matches on both email and domain. Loosen if you want to email multiple people per company.

## Apollo.io (free tier)

Sign up at [apollo.io](https://apollo.io), then **Settings → Integrations → API** to grab your key. Drop it into `.env` as `APOLLO_API_KEY`. The free plan is enough to drive this agent because:

- We **only call the search endpoint** — Apollo's free tier doesn't meter searches the way it meters email/phone unlocks.
- We **never request `reveal_personal_emails`** — that's the call that burns credits. If Apollo returns a verified email for free, we use it; if it's locked (`email_not_unlocked@…`), we fall back to pattern-guessing in [enrichment.py](outbound/enrichment.py).
- The `DAILY_SEND_CAP` already bounds how many leads ever leave the funnel each day.

Default search filters (tweak in [scrapers.py:fetch_apollo_founders](outbound/scrapers.py)):

- Titles: Founder, Co-Founder, CEO
- Headcount: 1–20
- Latest funding stage: Pre-Seed or Seed

Pass `keywords` from the user message to narrow further (e.g. "fintech B2B" or "developer tools").

## Adding a new source

1. Add a `fetch_<source>()` function in `scrapers.py` that returns the same dict shape:
   `source, source_ref, company_name, company_url, company_domain, one_liner, founder_name`.
2. Add a branch in `outbound/tools.py:discover_leads`.
3. The system prompt already handles arbitrary sources — no changes needed.

## How a send is recorded

A send is claimed *before* the transport call and confirmed after it, so a
crash in between leaves evidence rather than a silent gap:

| status | meaning | counts as contact? |
|---|---|---|
| `reserved` | slot claimed, outcome not yet known | **yes** |
| `sent` | transport accepted it | **yes** |
| `released` | transport gave positive evidence of rejection | no |

`reserved` counts deliberately. If the process dies mid-send we cannot know
whether the mail went out, and the two errors are not symmetric: emailing
someone twice is visible to them, and never emailing them again is not.

A slot is only released on a definitive refusal — an API 4xx, a rejected
recipient. Timeouts, 5xx responses and dropped connections are ambiguous, so
the reservation stands and the error is recorded on the row.

## What came back

`sends` records that a message was handed to a transport. It cannot say whether
it landed, bounced, or was answered — so `replies` does, and
`python -m outbound stats [days]` prints the four numbers worth reading:
sent, delivery confirmed, bounced, replies split by human / opt-out / auto.

Rates are printed against *sent*, and the report says how much of that was
delivery-confirmed, because a reply rate quoted against sends when a tenth
bounced is a flattering number — and flattering numbers are how a channel gets
kept alive past the point it should have been cut.

**Opens are not tracked, deliberately.** A tracking pixel needs consent under
ePrivacy, Apple Mail Privacy Protection makes the number fiction, and remote
images cost deliverability. A metric that is both unlawful and wrong is worse
than no metric. Message bodies are not stored either: a reply is a named
person's words about their own systems, and every field kept is a field to be
defended later.

## Verification

The agent writes a `runs` row at the start of every execution and updates it
at the end, with source, counts, status, cost and any error. Nothing else in
this repo can tell you whether this morning's run happened —
`>> outbound.log` is not an answer, because nobody reads it.

[`runproof`](https://github.com/smuzstudio/runproof) reads that ledger and asserts eleven properties
with no model involved: that a run started in the expected window, finished
`ok`, discovered at least one lead, left no lead abandoned, stayed under the
cap and the cost ceiling, sent nothing real from a rehearsal, recorded a
message id for every delivered mail, left no send awaiting its outcome, and
never emailed the same address twice.

```bash
cd ../runproof && python -m runproof check -p runproof/profiles/outbound.toml
```

It exits non-zero on failure, so it belongs on its own cron line — a check
nobody is paged about is a check nobody runs.

### Owner

**Nick Lysenko — `hello@smuz.io`.** A failing check mails the report there;
the destination is declared in the `[alert]` block of
`runproof/profiles/outbound.toml`, and an alert that cannot be delivered
exits `3` rather than passing quietly. Set the transport on the machine that
runs the check — the four variables are read from the environment, never from
the profile:

```bash
export RUNPROOF_SMTP_HOST=smtp.gmail.com
export RUNPROOF_SMTP_PORT=587
export RUNPROOF_SMTP_USER=hello@smuz.io
export RUNPROOF_SMTP_PASSWORD=…      # Google Workspace app password
```

Then prove the path once, before trusting it:

```bash
cd ../runproof && python -m runproof alert-test -p runproof/profiles/outbound.toml
```

Until that message has actually arrived, ownership is an intention rather
than a fact, and [`AUDIT.md`](AUDIT.md) marks it ⚠ accordingly.

## Tests

```bash
./.venv/bin/python -m unittest discover -s tests -t .
```

The cap test spawns twenty real processes against a cap of five. The bug it
guards was a race between a read and a write in separate transactions, and a
single-process test cannot observe a race.

## Compliance

- Every email carries the sender footer, the controller's registered postal
  address, an opt-out line and a `List-Unsubscribe` header (`sender.py:_with_footer`).
- **A real send refuses to run without `SENDER_POSTAL_ADDRESS`** (`IdentityMissing`).
  A commercial email to a named person must carry the sender's registered identity;
  a run that sent fifteen emails without it would report success and produce
  fifteen unlawful messages. Dry runs are exempt so rehearsals work before the
  identity is settled.
- **Opt-outs are a table, not a memory.** `suppressions` is checked before the
  daily cap is claimed, in both the send and the discovery path, matching on
  address *and* on whole domain — one person opting out speaks for the company.
  Removal deactivates the row rather than deleting it: the evidence that a
  request was honoured is the part that matters later. See AUDIT.md F10.

  ```bash
  python -m outbound suppress someone@acme.com unsubscribe "replied 7 Sep"
  python -m outbound suppress @acme.com complaint
  python -m outbound suppressions
  ```

  Replies are read by `python -m outbound ingest-replies [days]`, which
  classifies each inbound message (human / auto / bounce / unsubscribe) and
  **acts on an opt-out during ingestion** rather than writing it into a report
  someone is meant to read later. It re-reads the whole window every run and is
  idempotent on Message-ID — the seen flag belongs to whoever opened the mailbox
  on their phone, and the opt-out read on a phone is the one that must not be
  missed. Needs `IMAP_USER` / `IMAP_PASSWORD`; **verified by tests, not yet
  against a live mailbox**, because the Workspace app password does not exist
  yet.

  Two classifier rules are worth knowing, because both failures are silent:
  an out-of-office is *not* an opt-out, and only the first 600 characters of a
  body are searched — every reply quotes our own footer, which contains the
  word "unsubscribe", so reading the whole body would empty the list in a week.
- **Jurisdiction is enforced in code, not in the prompt** (`jurisdiction.py`).
  Whether unsolicited commercial email may be sent at all is national law and
  depends on the *recipient's* country: Poland, Germany and Austria require
  prior consent; the UK exempts corporate subscribers; the US is opt-out.
  `MARKETING_EXCLUDED_COUNTRIES` holds the refusal list, and **a lead with no
  recorded country is refused as well** — failing open would protect only the
  leads whose country happened to get recorded, and the gap would be invisible
  because every send would still look fine. The model records the country with
  `set_lead_country` from what the research actually showed.
- `already_contacted()` blocks repeat sends per-address and per-domain, and
  counts a reservation whose outcome is unknown as contact.
- Sender identity (name + reply-to + physical operator) lives in `.env`. CAN-SPAM and most equivalents require a real reply path and an opt-out — both are wired.
- The agent never fabricates social proof or mutual connections (enforced in the prompt; rejected sends won't show that pattern in dry-run, so review a few dry-runs before flipping `DRY_RUN=false`).
- Set `SENDER_REPLY_TO` to an inbox a human actually checks — opt-out replies land there.

## Operating it

Inspect the DB any time:

```bash
sqlite3 outbound.db
> select id, started_at, status, sent, cost_usd from runs order by id desc limit 10;
> select id, company_name, status, founder_email from leads order by id desc limit 20;
> select date(sent_at), status, count(*) from sends group by 1, 2;
> select id, to_email, error from sends where status = 'reserved';
```

That last query is the one to check after any run that ended badly: those are
sends whose outcome was never established.

To re-open a skipped lead:

```sql
UPDATE leads SET status='new', skip_reason=NULL WHERE id=42;
```

## What this agent is NOT

- Not a follow-up sequencer. First-touch only. Reply handling belongs in your inbox / CRM; the agent doesn't read mail.
- Not a CRM. The DB is a dedupe ledger, not a pipeline tool.
- Not LinkedIn / Twitter. Cold email only. Add a sender if you want more channels.
