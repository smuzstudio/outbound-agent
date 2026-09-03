"""CLI entrypoint: `python -m outbound.main` (or `python -m outbound`).

Default behavior: discover 10 fresh YC leads, research each, send what passes
qualification — in dry-run unless DRY_RUN=false. Override the discovery target
with arguments:

    python -m outbound.main yc W25 10
    python -m outbound.main producthunt - 5
    python -m outbound.main apollo - 10

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


def main() -> None:
    argv = sys.argv[1:]
    source = (argv[0] if len(argv) > 0 else "yc").lower()
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
