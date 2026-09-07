"""CLI entrypoint: `python -m outbound.main` (or `python -m outbound`).

Default behavior: discover 10 leads from the researched cohort, research each,
send what passes qualification — in dry-run unless DRY_RUN=false. Override the
discovery target with arguments:

    python -m outbound.main targets - 10
    python -m outbound.main yc W25 10          # founder-era, dry-run only
    python -m outbound.main producthunt - 5    # founder-era, dry-run only
    python -m outbound.main apollo - 10        # founder-era, dry-run only

The model interprets the args via the user message we hand it.
"""
from __future__ import annotations

import asyncio
import fcntl
import sys
from contextlib import contextmanager
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    create_sdk_mcp_server,
    query,
)
from rich.console import Console
from rich.rule import Rule

from .config import settings
from .prompts import system_prompt
from . import storage
from .storage import finish_run, init_db, run_counts, sends_today, start_run
from .tools import ALL_TOOLS, ALLOWED_TOOL_NAMES


console = Console()


def _user_request(source: str, batch: str | None, n: int) -> str:
    batch_clause = f" from batch {batch}" if (source == "yc" and batch) else ""
    return (
        f"Run today's outreach. Discover {n} leads from {source}{batch_clause}, "
        f"research each, and send a personalized cold email to every lead that "
        f"qualifies. Skip the rest with a one-word reason. "
        f"When you're done, show me a summary."
    )


async def _run(source: str, batch: str | None, n: int) -> None:
    init_db()
    run_id = start_run(
        source=source, requested_n=n, dry_run=settings.dry_run,
        daily_cap=settings.daily_send_cap,
    )
    console.print(Rule(f"Smuz outbound  ·  run {run_id}  ·  {source}  ·  cap {settings.daily_send_cap}/day"))
    console.print(
        f"[dim]dry_run={settings.dry_run}  sent_today={sends_today()}  "
        f"db={settings.db_path}[/dim]\n"
    )

    server = create_sdk_mcp_server(name="outbound", tools=ALL_TOOLS)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt(),
        mcp_servers={"outbound": server},
        allowed_tools=ALLOWED_TOOL_NAMES,
        # No filesystem / shell tools — this agent only does outreach.
        permission_mode="acceptEdits",
    )

    cost: float | None = None
    try:
        async for msg in query(prompt=_user_request(source, batch, n), options=options):
            # The SDK yields a stream of typed messages. We surface text + tool
            # use so the operator can watch the run live.
            cost = _print_message(msg) or cost
    except Exception as exc:  # noqa: BLE001 — the ledger must record *why*
        finish_run(run_id, status="error", cost_usd=cost, error=f"{type(exc).__name__}: {exc}")
        console.print(f"[red]run {run_id} failed: {exc}[/red]")
        raise

    finish_run(run_id, status="ok", cost_usd=cost)
    counts = run_counts(run_id)
    console.print(
        f"[dim]run {run_id}: discovered={counts['discovered']} "
        f"sent={counts['sent']} skipped={counts['skipped']} "
        f"pending={counts['pending']}[/dim]"
    )


def _print_message(msg) -> float | None:
    """Print a streamed message. Returns the run cost when the result arrives."""
    # The SDK message shapes are stable: AssistantMessage(content=[...]),
    # UserMessage (tool results), ResultMessage at end. We duck-type to avoid
    # import gymnastics across SDK versions.
    kind = type(msg).__name__
    content = getattr(msg, "content", None)

    if kind == "ResultMessage":
        usage = getattr(msg, "usage", None)
        cost = getattr(msg, "total_cost_usd", None)
        console.print(Rule("done"))
        if cost is not None:
            console.print(f"[dim]cost: ${cost:.4f}  usage: {usage}[/dim]")
        return cost

    if not content:
        return None
    for block in content:
        btype = type(block).__name__
        if btype == "TextBlock":
            text = getattr(block, "text", "")
            if text.strip():
                console.print(text)
        elif btype == "ToolUseBlock":
            name = getattr(block, "name", "?").replace("mcp__outbound__", "")
            console.print(f"[cyan]→ {name}[/cyan]  [dim]{getattr(block, 'input', {})}[/dim]")
        elif btype == "ToolResultBlock":
            # Tool results are echoed back to the model; no need to dump full
            # JSON to the operator unless debugging.
            pass
    return None


@contextmanager
def _single_instance(lock_path: Path):
    """Yield True if this process holds the run lock, False if another does.

    The atomic cap in storage.reserve_send() makes concurrent runs *safe*;
    this makes them *impossible*, which is cheaper to reason about. Cron
    double-firing, a manual run started while the scheduled one is going, a
    retry wrapper — all collapse to one running process.

    The lock is released by the OS when the process dies, so a crash cannot
    wedge tomorrow's run.
    """
    handle = open(lock_path, "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _cmd_suppress(argv: list[str]) -> int:
    """`suppress <email|@domain> [reason] [note...]` — honour an opt-out.

    Deliberately a first-class command rather than a SQL snippet in a note
    somewhere: the footer promises removal, and the promise needs a mechanism
    a tired person can run in one line.
    """
    if not argv:
        console.print("usage: python -m outbound suppress <email|@domain> "
                      "[unsubscribe|bounce|complaint|manual|jurisdiction] [note]",
                      markup=False)
        return 2
    target = argv[0].strip().lower()
    reason = argv[1] if len(argv) > 1 else "unsubscribe"
    note = " ".join(argv[2:]) or None
    storage.init_db()
    try:
        if target.startswith("@"):
            storage.suppress(domain=target[1:], reason=reason, source="cli", note=note)
            console.print(f"[green]suppressed domain[/green] {target[1:]} ({reason})")
        else:
            storage.suppress(email=target, reason=reason, source="cli", note=note)
            console.print(f"[green]suppressed[/green] {target} ({reason})")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    return 0


def _cmd_suppressions(argv: list[str]) -> int:
    storage.init_db()
    rows = storage.list_suppressions(active_only="--all" not in argv)
    if not rows:
        console.print("[yellow]no suppressions recorded[/yellow]")
        return 0
    for r in rows:
        scope = r["email"] or f"@{r['domain']}"
        flag = "" if r["active"] else " (inactive)"
        console.print(f"{r['created_at'][:10]}  {scope}  — {r['reason']}{flag}")
    console.print(f"\n{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    return 0


def _cmd_stats(argv: list[str]) -> int:
    """`stats [days]` — the four numbers, and what is not measured."""
    days = int(argv[0]) if argv and argv[0].isdigit() else 30
    storage.init_db()
    st = storage.outbound_stats(days)
    console.print(f"[bold]last {st['days']} days[/bold]")
    console.print(f"  sent                {st['sent']}")
    console.print(f"  delivery confirmed  {st['delivery_confirmed_for']}")
    console.print(f"  bounced             {st['bounced']}")
    console.print(f"  replies (human)     {st['replies_human']}")
    console.print(f"  replies (opt-out)   {st['replies_unsubscribe']}")
    console.print(f"  replies (auto)      {st['replies_auto']}")
    console.print(f"  suppressed, active  {st['suppressions_active']}")
    if st["sent"]:
        console.print(f"\n  bounce rate {st['bounce_rate_of_sent']:.1%} of sent"
                      f" · reply rate {st['reply_rate_of_sent']:.1%} of sent")
    console.print("\n[dim]Opens are not tracked, on purpose: a pixel needs consent, "
                  "Apple MPP makes the number fiction, and remote images cost "
                  "deliverability.[/dim]")
    return 0


def _cmd_ingest_replies(argv: list[str]) -> int:
    """`ingest-replies [days]` — read the mailbox, record, and honour opt-outs."""
    from . import inbox
    days = int(argv[0]) if argv and argv[0].isdigit() else 7
    storage.init_db()
    try:
        client = inbox.connect()
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    try:
        messages = inbox.fetch_recent(client, days=days)
    finally:
        try:
            client.logout()
        except Exception:
            pass

    counts: dict[str, int] = {}
    suppressed = 0
    for msg in messages:
        result = inbox.ingest(msg)
        counts[result["kind"]] = counts.get(result["kind"], 0) + 1
        if result.get("suppressed"):
            suppressed += 1
            console.print(f"[yellow]suppressed[/yellow] {result['from']} "
                          f"({result['kind']})")
    console.print(f"{len(messages)} message(s) in {days}d: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                  + f" · {suppressed} newly suppressed")
    return 0


COMMANDS = {
    "suppress": _cmd_suppress,
    "suppressions": _cmd_suppressions,
    "stats": _cmd_stats,
    "ingest-replies": _cmd_ingest_replies,
}


# Discovery sources aimed at the founder-era ICP (pre-seed founders, <=25 people).
# Kept because they work and cost nothing to keep; fenced to dry runs because the
# company they find is not the company we sell to. See README.
FOUNDER_ERA_SOURCES = frozenset({"yc", "producthunt", "apollo"})
DEFAULT_SOURCE = "targets"


def main() -> None:
    argv = sys.argv[1:]

    if argv and argv[0] in COMMANDS:
        sys.exit(COMMANDS[argv[0]](argv[1:]))

    source = (argv[0] if len(argv) > 0 else DEFAULT_SOURCE).lower()

    if source in FOUNDER_ERA_SOURCES and not settings.dry_run:
        # These three discover the previous ICP: YC/ProductHunt launches and
        # Apollo founder search at 1-20 employees, with the YC path discarding
        # anything over 25 people. The current ICP is the inverse — 50-500
        # employees, something running unattended on a schedule — so a live run
        # from here sends a correct pitch to the wrong companies, and burns the
        # address while doing it. The README says nothing should be sent from
        # them; this is that sentence in code, because a guarantee the code does
        # not enforce is exactly what F4 of the audit was.
        console.print(
            f"[red]{source!r} discovers the previous ICP — refusing to send.[/red]\n"
            f"Use [bold]targets[/bold] for the researched cohort, or set DRY_RUN=true "
            f"to inspect what {source!r} would return."
        )
        sys.exit(2)

    batch = argv[1] if len(argv) > 1 and argv[1] != "-" else None
    n = int(argv[2]) if len(argv) > 2 else 10

    if not settings.anthropic_api_key:
        console.print("[red]Missing ANTHROPIC_API_KEY in .env[/red]")
        sys.exit(1)

    lock_path = Path(settings.db_path).expanduser().with_suffix(".lock")
    with _single_instance(lock_path) as acquired:
        if not acquired:
            # Not an error: the scheduled run is already doing the work.
            # A genuinely missing run is caught by verification, not here.
            console.print("[yellow]another run holds the lock — exiting[/yellow]")
            return
        asyncio.run(_run(source, batch, n))


if __name__ == "__main__":
    main()
