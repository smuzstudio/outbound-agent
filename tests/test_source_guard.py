"""The founder-era discovery sources may be looked at, never sent from.

`scrapers.py` finds YC and ProductHunt launches and Apollo founders at 1-20
employees, and its YC path drops anything over 25 people. The current ICP is
the inverse: 50-500 employees with something running unattended on a schedule.
So those three return real, well-formed leads that are the wrong companies —
a failure that is silent by construction, which is the kind this repo exists
to catch.

The README already says nothing should be sent from them. This asserts the
code says it too, because a guarantee only the README enforces is F4.
"""
from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from outbound import main as main_mod  # noqa: E402


def _exit_code(argv: list[str], *, dry_run: bool) -> int | None:
    """Run the CLI with no API key and return its exit code.

    The missing key exits 1 immediately after the guard and before the agent
    starts, so: 2 means the guard fired, 1 means it let the source through.
    """
    settings = dataclasses.replace(
        main_mod.settings, dry_run=dry_run, anthropic_api_key=""
    )
    with mock.patch.object(main_mod, "settings", settings), \
            mock.patch.object(sys, "argv", ["outbound", *argv]):
        try:
            main_mod.main()
        except SystemExit as exc:
            return exc.code
    return None


class FounderEraSourcesAreFenced(unittest.TestCase):
    def test_live_run_from_a_founder_era_source_is_refused(self) -> None:
        for source in sorted(main_mod.FOUNDER_ERA_SOURCES):
            with self.subTest(source=source):
                self.assertEqual(_exit_code([source], dry_run=False), 2)

    def test_dry_run_may_still_inspect_them(self) -> None:
        """Refusing to look as well would be deleting them, not fencing them."""
        for source in sorted(main_mod.FOUNDER_ERA_SOURCES):
            with self.subTest(source=source):
                self.assertEqual(_exit_code([source], dry_run=True), 1)

    def test_the_default_source_is_not_a_founder_era_one(self) -> None:
        """`python -m outbound` with no arguments is what cron runs."""
        self.assertNotIn(main_mod.DEFAULT_SOURCE, main_mod.FOUNDER_ERA_SOURCES)
        self.assertEqual(main_mod.DEFAULT_SOURCE, "targets")
        self.assertEqual(_exit_code([], dry_run=False), 1)


if __name__ == "__main__":
    unittest.main()
