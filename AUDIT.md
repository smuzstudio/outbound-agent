# Fleet Audit — `outbound-agent`

**Subject:** Smuz outbound agent (single agent, cron-scheduled, weekdays 09:00)
**Date:** 2026-08-29 · **Remediation verified:** 2026-08-31, 2026-09-03
**Method:** read-only source inspection. No execution, no production data, no changes.
**Auditor:** Smuz

This is a real audit of our own agent, published in full — including what it found. It doubles as the reference format for a Smuz Fleet Audit. Reuse notes are at the end.

---

## 0. The question this audit asks

Not "is the code good." Four questions, in order, because each one is worthless without the one before it:

| | Question | Why it's separate |
|---|---|---|
| **Liveness** | Did it run? | A perfect agent that isn't firing is a dead agent. |
| **Throughput** | Did it do work? | A run that processes zero records exits successfully and looks identical to a healthy one. |
| **Effect** | Did the work land? | "The API call returned 200" is not "the thing happened." |
| **Ownership** | Would anyone know if it hadn't? | An unowned failure is an indefinite failure. |

An agent passes only if all four are answerable **from data, by a check containing no model**.

---

## 1. Summary

| Agent | Liveness | Throughput | Effect | Ownership | Verdict |
|---|---|---|---|---|---|
| `outbound` (daily, 09:00 Mon–Fri) — **as audited, 29 Aug** | ✗ | ✗ | ⚠ | ✗ | **Not verifiable** |
| `outbound` — **after remediation, 31 Aug** | ✓ | ✓ | ✓ | ⚠ | **Verifiable; not yet owned** |
| `outbound` — **after alerting, 3 Sep** | ✓ | ✓ | ✓ | ⚠ | **Verifiable; owner named, delivery not yet proven** |

**One agent inspected. As audited it could not answer any of the four
questions.** All nine findings now have a fix in place and the assertion set
of §4 runs against it as [`runproof`](https://github.com/smuzstudio/runproof), which since 3 Sep also
mails a failing report to a named human at `hello@smuz.io`.

Ownership stays ⚠ anyway, and the reason is worth stating plainly: **the
alert path has been implemented and unit-tested, but no alert has yet been
delivered.** A configured destination is a claim about the future. Ownership
is not established by a block in a config file — that would be a check
verifying itself against its own configuration, which this audit rejects
everywhere else — but by a message actually arriving. The column flips to ✓
on the date the first `runproof alert-test` lands in the mailbox, and not
before. See F5.

The agent is thoughtfully built — the dedupe, the cap, the dry-run default and the compliance footer all show someone who has thought about consequences. But **none of its safety properties are verifiable after the fact, and two of them do not hold.** The README documents a guarantee the code does not provide.

**9 findings: 3 critical, 3 high, 3 medium.**

| # | Finding | Class | Severity | Status |
|---|---|---|---|---|
| F1 | Dry runs are written to `sends`, permanently poisoning dedupe and the daily cap | Effect | **Critical** | **Fixed 2026-08-29** |
| F2 | No run ledger — nothing records that a run happened | Liveness | **Critical** | **Fixed 2026-08-31** |
| F3 | Zero-work runs exit 0, indistinguishable from successful runs | Throughput | **Critical** | **Fixed 2026-08-31** |
| F4 | Daily cap is a check-then-act race; the documented guarantee is false | Effect | High | **Fixed 2026-08-31** |
| F5 | No alerting and no named owner | Ownership | High | **Fixed 2026-09-03; delivery unproven** |
| F6 | A mid-run send failure aborts the run with no checkpoint | Throughput | High | **Fixed 2026-08-31** |
| F7 | SMTP sends always record `provider_message_id = NULL` | Effect | Medium | **Fixed 2026-08-31** |
| F8 | Send-then-record is not atomic in the failure direction | Effect | Medium | **Fixed 2026-08-31** |
| F9 | Per-run cost is printed and discarded | Ownership | Medium | **Fixed 2026-08-31** |

---

## 1b. What changed, and how it was checked

Remediation is listed here rather than folded into each finding, so the audit
still reads as it did on the day it was written.

| | Fix | Evidence it works |
|---|---|---|
| F2, F9 | `runs` table written at start and updated at finish, carrying source, status, cost and error. `run_id` stamped on `leads` and `sends` | Throughput is *counted from rows* rather than read from a number the run reported about itself — a run cannot lie about its own output |
| F3 | Throughput floor asserted outside the agent (A3) | `test_zero_work_run_exits_ok` — a run that discovers nothing, crashes nothing and exits 0 now fails verification |
| F4 | Count and claim moved into one `BEGIN IMMEDIATE` transaction; exclusive `flock` on the run lock makes concurrent runs impossible as well as safe | `test_concurrent_processes_cannot_oversend`: 20 processes, cap 5 → exactly 5. The same load against the old check-then-act shape produces 9 |
| F6, F8 | Slot claimed before the transport call, confirmed after. Transport errors are returned to the model as tool results instead of aborting the run | `test_unconfirmed_reservation_still_counts`, `test_released_send_does_not_count`, and A11 |
| F7 | Message id generated with `make_msgid()` and set on the message *before* sending | A7 asserts every delivered mail carries one |
| F5 | Assertion runner mails the failing report to a named owner; an undelivered alert exits `3`, distinct from both pass and fail | 15 tests in `runproof/tests/test_alert.py` break delivery specifically — missing credentials, refused connection, half-configured block. **No live send yet** — see below |

**On F6/F8 and the ambiguous case.** A send is claimed before the transport
call and confirmed after. A reservation whose outcome is unknown — timeout,
5xx, dropped connection — is left standing and *counts as contact*. Only a
definitive refusal frees the slot. The asymmetry is deliberate: emailing
someone twice is visible to the recipient, and never emailing them again is
not. Severity here is consequence × undetectability, and it points one way.

**On F5, and why ⚠ is still the honest mark.** The mechanism now exists:
eleven assertions run, a failing report is mailed to `hello@smuz.io`, and
`runproof/profiles/outbound.toml` names Nick Lysenko as the human who answers
it. Three properties of that path are deliberate:

- **An undelivered alert exits `3`,** not `0` and not `1`. An alerter that
  fails quietly converts a loud failure into a silent one — the precise
  substitution this whole document is about.
- **Credentials are not in the profile.** The profile is committed and
  published; the transport reads them from the environment, and their
  absence is an error rather than a silent skip.
- **The path is exercised on the good day.** `runproof alert-test` sends on
  demand and runs weekly, because a path that only fires when something is
  broken is untested every other day, and an expired app password is
  discovered at the worst possible moment.

What is still missing is the only thing that actually settles the question:
**a message that has arrived.** Until the first `alert-test` is delivered,
this is a well-tested intention. Ownership is the one dimension that cannot
be closed by writing code, which is exactly why it is the one most often
marked green without evidence.

---

## 2. Findings

### F1 — Dry runs permanently burn leads and consume the real send cap · **Critical**

**Evidence.** [`sender.py:32`](outbound/sender.py) returns `provider: "dryrun"` when `DRY_RUN=true`. [`tools.py:165`](outbound/tools.py) calls `sender.send_email(...)` and then calls `storage.record_send(...)` **unconditionally** on the next line — a row lands in `sends` whether or not anything was sent. Neither [`already_contacted()`](outbound/storage.py) (`storage.py:133`) nor [`sends_today()`](outbound/storage.py) (`storage.py:169`) filters on `provider`.

**Failure scenario.** You follow the README, run three days of dry runs against 45 YC leads to review the copy, then set `DRY_RUN=false`. Every one of those 45 leads is now permanently un-emailable, **and so is every other person at their domain** — `already_contacted` matches on domain via the join at `storage.py:146`. Your first real run silently skips them all.

**Why it's the worst finding here.** The symptom looks like correct behaviour. The agent reports `status: skipped, reason: already_contacted`, which is exactly what a healthy dedupe emits. Nothing in the output distinguishes "correctly skipped a real prior contact" from "destroyed a lead because of a rehearsal." You would conclude the dedupe is working.

**Fix — applied 2026-08-29.** `DRYRUN_PROVIDER` is now a single constant in `config.py`, and both `already_contacted()` and `sends_today()` exclude rows carrying it. Dry-run rows are still written, so a rehearsal remains reviewable — they simply no longer count as contact.

Verified against a temporary database: three dry runs to the same address leave `already_contacted` false for both address and domain and `sends_today` at 0; a subsequent real send flips both to true, blocks other addresses at the same domain, and increments the cap by exactly one. All four rows are retained.

The fix is retroactive — existing polluted databases heal on the next query, because the filter matches on `provider` rather than on when the row was written. No migration needed.

---

### F2 — No run ledger · **Critical**

**Evidence.** The schema at `storage.py:13–48` defines `leads` and `sends`. There is no `runs` table. `main.py:45–65` persists nothing about the execution itself — not start time, not finish, not source, not counts, not exit status.

**Failure scenario.** Someone asks "did outreach go out this morning?" There is no way to answer from data. The only evidence is `outbound.log`, appended to by the cron line in the README, which nobody reads. If crontab is edited, the venv path breaks, the machine sleeps, or `ANTHROPIC_API_KEY` expires (`main.py:106` exits 1 — into a log file), the agent stops and **the observable state is identical to a day with no qualifying leads.**

**Fix.** A `runs` table: `id, started_at, finished_at, source, requested_n, discovered, sent, skipped, status, cost_usd, error`. Written at start and updated at finish. Everything else in this audit becomes checkable once this exists.

---

### F3 — Zero-work runs exit 0 · **Critical**

**Evidence.** `main.py:100–110` runs the agent loop and returns. No assertion on outcome. `discover_leads` (`tools.py:34`) returns `{"inserted": 0, "leads": []}` on an empty upstream result — a normal tool response, not an error.

**Failure scenario.** The `yc-oss` JSON mirror changes its schema or goes offline. `fetch_yc_companies` returns `[]`. The agent discovers nothing, sends nothing, prints a polite summary, exits 0. Cron records success. This repeats every weekday. You find out when you notice the reply volume dropped — typically weeks later, and you will first suspect your copy.

**This is the failure class the whole product exists for.** It is not an exception, not a crash, not a stack trace. It is success with an empty payload.

**Fix.** A throughput floor asserted outside the agent (A3).

---

### F4 — The daily cap is a race, and the README claims otherwise · **High**

**Evidence.** `tools.py:160–163` reads `sends_today()`, compares against the cap, and then sends at `:165`. `storage.py:56–63` opens a **new connection per call**, so the read and the subsequent insert are in different transactions with nothing spanning them.

The README states: *"The daily cap is enforced by the agent itself (`send_email` returns an error after the cap), so even if cron double-fires nothing oversends."*

**That guarantee does not hold.** Two concurrent runs both read `n < cap` and both proceed. Cron double-firing is the exact scenario the README names as covered.

**Why it matters beyond the bug.** A documented safety guarantee that the code does not provide is worse than no guarantee, because it stops anyone from checking.

**Fix.** Either make the cap atomic (single transaction with a conditional insert, or a `UNIQUE` day-counter row), or take a lockfile at process start to make double-firing impossible. Then assert the outcome independently (A5) rather than trusting the in-process check.

---

### F5 — No alerting, no owner · **High**

**Evidence.** The README's cron line ends `>> ./outbound.log 2>&1`. Failures, tracebacks, and the `Missing ANTHROPIC_API_KEY` exit at `main.py:107` all go to a file. Nothing notifies a person. No owner is named anywhere in the repo.

**Fix.** One assertion runner, on its own schedule, writing to somewhere a human actually looks. Name the human in the repo.

**Applied 2026-09-03.** `runproof` grew an `[alert]` block: destination
`hello@smuz.io`, owner named in the profile, mail sent on any failing check,
and exit code `3` when the mail itself cannot be sent. A profile with no
`[alert]` renders its owner as `UNASSIGNED` rather than omitting the line, so
an unowned check looks unowned on the page. Proven by tests, not yet by a
delivered message — see §1b.

---

### F6 — A mid-run send failure aborts with no checkpoint · **High**

**Evidence.** `sender.py:59` calls `r.raise_for_status()`. A Resend 429 or 5xx raises inside the tool, mid-run, after earlier leads have already been sent and recorded.

**Failure scenario.** Lead 7 of 15 hits a rate limit. The run dies. Leads 1–6 were emailed. Leads 8–15 were never touched, but their `status` is still `researched`, so a rerun re-processes them — correct by luck, via the dedupe, not by design. There is no record of where the run stopped or that it stopped at all (see F2).

**Fix.** Catch transport errors in the tool and return them as tool results so the model can skip and continue; record the failure on the lead row.

---

### F7 — SMTP sends never record a message id · **Medium**

**Evidence.** `sender.py:76` returns `msg["Message-ID"]`, read from the `EmailMessage` **after** `send_message()`. `EmailMessage` does not populate `Message-ID` itself and `smtplib.send_message()` does not add one — the receiving MTA does. The expression is always `None`.

**Consequence.** Every SMTP send stores `provider_message_id = NULL`. You cannot correlate a database row with a delivered message, which makes any bounce or complaint investigation impossible after the fact.

**Fix.** Generate the id yourself with `email.utils.make_msgid()` and set it on the message before sending.

---

### F8 — Send-then-record is not atomic · **Medium**

**Evidence.** `tools.py:165` sends; `tools.py:166` records. If `record_send` throws — SQLite lock, disk — the email has left and there is no row.

**Consequence.** The inverse of F1: a real recipient who is not deduped and not counted, and who can therefore be emailed again.

**Fix.** Write an intent row before sending and mark it confirmed after, so a crash leaves evidence in the safe direction.

---

### F9 — Cost is printed and discarded · **Medium**

**Evidence.** `main.py:79–80` prints `total_cost_usd` from the `ResultMessage` to the console. Nothing persists it.

**Consequence.** No spend history, so a runaway loop — a model retrying research on a site that always times out — is invisible until the invoice.

**Fix.** Persist `cost_usd` on the run row (F2) and assert a per-run ceiling (A9).

---

## 3. What's already right

An audit that finds only problems is a sales document. These are genuinely good and should not be changed:

- **`DRY_RUN=true` is the default** (`config.py:41`). The correct default, spoiled only by F1.
- **Dedupe on both address and domain** (`storage.py:133`). Most first versions do neither.
- **The tool surface is deliberately narrow** — no filesystem or shell tools (`main.py:58`), so the agent's blast radius is bounded by design.
- **Unsubscribe line and footer appended in the transport** (`sender.py:17`), not left to the model. Compliance that can't be prompted away.
- **A written "What this agent is NOT" section** in the README. Scope discipline in prose is rare and it is why this audit was quick.

---

## 4. Proposed assertion set

Ten deterministic checks. **No model in any of them.** Each is a SQL query or a file stat, and each fails loudly with a run id and the specific assertion.

| | Assertion | Catches |
|---|---|---|
| **A1** | A `runs` row exists with `started_at` inside today's expected window (weekdays, 09:00 ±30m) | Cron removed, machine asleep, path broken |
| **A2** | That row has `finished_at` and `status='ok'` | Crash, mid-run abort (F6) |
| **A3** | `discovered >= 1` for the run | Silent no-op, upstream source dead (F3) |
| **A4** | `discovered == sent + skipped + still_new` | Leads vanishing between stages |
| **A5** | `COUNT(sends WHERE provider != 'dryrun' AND date = today) <= DAILY_SEND_CAP` | Cap race (F4), asserted independently of the in-agent check |
| **A6** | If `DRY_RUN=false`: zero `provider='dryrun'` rows today. If `true`: zero rows with `provider != 'dryrun'` | Mode confusion, and F1 once fixed |
| **A7** | Every non-dryrun send today has a non-null `provider_message_id` | F7, and silent transport degradation |
| **A8** | No `to_email` appears more than once across all `sends` | Dedupe regression — the highest-consequence failure, because the recipient sees it |
| **A9** | `cost_usd <= ceiling` for the run | Runaway loop (F9) |
| **A10** | Most recent `status='ok'` run is under 26 hours old on a weekday | Everything above, as a backstop |
| **A11** | No send from a finished run is still `reserved` | The crash window between claiming a send slot and confirming its outcome (F8) |

**A8 is the one that matters most.** It is the only assertion whose failure is visible to someone outside the company.

Note what these do *not* do: they say nothing about whether the emails were any good. Output quality is an evaluation problem. Whether the job happened is a verification problem. Conflating them is how monitoring gets sold badly.

---

## 5. Remediation

| | Work | Est. |
|---|---|---|
| 1 | ~~Fix F1 — filter `provider` in dedupe and cap~~ | ~~30 min~~ · done 29 Aug |
| 2 | ~~Add the `runs` table and write to it (F2, F9)~~ | ~~2 h~~ · done 31 Aug |
| 3 | Assertion runner + A1–A11 ✅ · alerting to a named human ✅ 3 Sep · **first live `alert-test` — outstanding** | 4 h · ~3.9 h done |
| 4 | ~~Make the cap atomic, add a lockfile (F4)~~ | ~~1 h~~ · done 31 Aug |
| 5 | ~~Catch transport errors, checkpoint the run (F6, F8)~~ | ~~2 h~~ · done 31 Aug |
| 6 | ~~Generate the SMTP message id (F7)~~ | ~~15 min~~ · done 31 Aug |
| 7 | ~~Correct the README's cap claim~~ | ~~10 min~~ · done 31 Aug |

**~10 hours estimated; roughly 10 minutes of it remains** — exporting the
four `RUNPROOF_SMTP_*` variables on the machine that runs the check and
watching one `alert-test` arrive. That message, not this paragraph, is what
turns the Ownership column green.

---

## 6. Reusing this format

The structure is agent-count-independent. For a fleet:

- **§1** becomes one row per agent, same four columns. The table is the deliverable a CTO reads; everything else is evidence.
- **§2** stays ordered by severity, never by discovery order. Every finding needs: evidence at `file:line`, a concrete failure scenario with real inputs, and the fix. A finding without a scenario is an opinion.
- **§3** is not padding. It establishes that the auditor can tell good from bad, which is what makes §2 credible.
- **§4** is the actual product. The assertions should be derivable from the findings — if an assertion doesn't trace to a finding or a named risk, cut it.
- **§5** carries hours, not prices. Pricing is a separate conversation and mixing them makes the report read as a pitch.

**Severity rule:** severity is consequence × *un*detectability. A loud crash is lower severity than a quiet wrong answer, because someone already knows about the crash. F1 outranks everything here not because it's the biggest bug but because its symptom is indistinguishable from correct behaviour.
