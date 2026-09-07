# Smuz outbound agent

A Claude-Agent-SDK workflow that finds early-stage founders on YC and ProductHunt, researches each one, and sends a personalized cold email pitching Smuz.

```
discover_leads  →  research_lead  →  send_email | skip_lead
                    (LLM writes the email itself,
                     no templates)
```

Single sender (`hello@smuz.io`), single product (Smuz), single goal: book a 30-minute strategy call. The model is the orchestrator AND the copywriter — the prompt at `outbound/prompts.py` is where you tune voice.

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
# Default: 10 YC leads, most-recent batches
python -m outbound

# Specific YC batch
python -m outbound yc W25 10

# Recent ProductHunt launches
python -m outbound producthunt - 5

# Apollo.io search (requires APOLLO_API_KEY; free tier works)
python -m outbound apollo - 10
```

Each run streams progress: the LLM calls `discover_leads`, then for each lead calls `research_lead`, then either `send_email` (with its own subject + body) or `skip_lead`. You see one line per step.

## Daily cron

A conservative outreach cadence — 15 emails/day, weekdays only:

```cron
# crontab -e
# the agent
0  9 * * 1-5  cd ~/Desktop/Smuz/outbound-agent && /usr/bin/env -S DAILY_SEND_CAP=15 ./.venv/bin/python -m outbound yc - 15 >> ./outbound.log 2>&1
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
| `outbound/scrapers.py` | YC (via `yc-oss` JSON mirror), ProductHunt (RSS, GraphQL fallback), Apollo.io (free-tier search). |
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

  Reply-reading is still manual: someone has to see the reply and run that
  command. Automatic ingestion is not built, and saying so is cheaper than
  discovering it.
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
