"""Tests for the jurisdiction gate.

Each test breaks one specific way the rule could quietly stop applying. The
important one is `test_unknown_country_is_refused`: a gate that fails open
protects only the leads whose country happened to get recorded, and the gap
is invisible because every send still looks fine.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload(name: str):
    from outbound import config
    importlib.reload(config)
    return importlib.reload(importlib.import_module(f"outbound.{name}"))


class JurisdictionTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["MARKETING_EXCLUDED_COUNTRIES"] = "PL,DE,AT"
        self.j = _reload("jurisdiction")

    def test_consent_required_country_is_refused(self) -> None:
        allowed, why = self.j.may_send("PL")
        self.assertFalse(allowed)
        self.assertEqual(why, "jurisdiction_excluded:PL")

    def test_opt_out_country_is_allowed(self) -> None:
        self.assertEqual(self.j.may_send("GB"), (True, "ok"))
        self.assertEqual(self.j.may_send("US"), (True, "ok"))

    def test_unknown_country_is_refused(self) -> None:
        """Failing open here would be the same as having no rule at all."""
        allowed, why = self.j.may_send(None)
        self.assertFalse(allowed)
        self.assertEqual(why, "jurisdiction_unknown")

    def test_unrecognised_name_is_refused_rather_than_guessed(self) -> None:
        allowed, why = self.j.may_send("Republic of Somewhere")
        self.assertFalse(allowed)
        self.assertEqual(why, "jurisdiction_unknown")

    def test_country_names_and_case_are_accepted(self) -> None:
        self.assertEqual(self.j.normalise(" poland "), "PL")
        self.assertEqual(self.j.normalise("United Kingdom"), "GB")
        self.assertEqual(self.j.normalise("de"), "DE")

    def test_the_excluded_list_is_configurable(self) -> None:
        os.environ["MARKETING_EXCLUDED_COUNTRIES"] = "FR"
        j = _reload("jurisdiction")
        self.assertTrue(j.may_send("PL")[0])
        self.assertFalse(j.may_send("FR")[0])
        os.environ["MARKETING_EXCLUDED_COUNTRIES"] = "PL,DE,AT"


class PromptTests(unittest.TestCase):
    """The prompt must not promise proof that isn't public yet."""

    def _prompt(self, **env):
        for k, v in env.items():
            os.environ[k] = v
        from outbound import config
        importlib.reload(config)
        prompts = importlib.reload(importlib.import_module("outbound.prompts"))
        return prompts.system_prompt()

    def test_no_audit_link_when_the_repo_is_still_private(self) -> None:
        text = self._prompt(AUDIT_URL="")
        self.assertIn("NO public link", text)
        self.assertNotIn("github.com/smuzstudio", text)

    def test_audit_link_appears_once_configured(self) -> None:
        url = "https://github.com/smuzstudio/outbound-agent/blob/main/AUDIT.md"
        self.assertIn(url, self._prompt(AUDIT_URL=url))
        os.environ["AUDIT_URL"] = ""

    def test_dead_offer_is_gone(self) -> None:
        text = self._prompt(AUDIT_URL="")
        for dead in ("Starter", "Growth", "Scale", "strategy call", "pre-seed"):
            self.assertNotIn(dead, text, f"founder-era copy resurfaced: {dead}")

    def test_live_offer_is_present(self) -> None:
        text = self._prompt(AUDIT_URL="")
        for live in ("$1,000", "$400/mo", "schedule that nobody watches", "no AI"):
            self.assertIn(live, text)


if __name__ == "__main__":
    unittest.main()
