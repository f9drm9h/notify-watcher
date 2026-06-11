"""Tests for the WWDC watcher's pure helpers (notify_watcher.topics.wwdc)."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

from notify_watcher.topics import wwdc

IN_WEEK = wwdc.WWDC_WEEK[0]  # first day of the configured WWDC week
OFF_WEEK = wwdc.WWDC_WEEK[1] + dt.timedelta(days=30)


class TitleMatchesTest(unittest.TestCase):
    def test_wwdc_keyword_matches_year_round(self):
        self.assertTrue(wwdc._title_matches("Apple announces WWDC26 dates", OFF_WEEK))
        self.assertTrue(wwdc._title_matches(
            "Worldwide Developers Conference kicks off", OFF_WEEK))

    def test_announcement_verbs_match_only_inside_wwdc_week(self):
        self.assertTrue(wwdc._title_matches("Apple unveils iOS 26", IN_WEEK))
        self.assertFalse(wwdc._title_matches("Apple unveils iOS 26", OFF_WEEK))

    def test_unrelated_titles_never_match(self):
        self.assertFalse(wwdc._title_matches("Apple Store opens in Delhi", IN_WEEK))
        self.assertFalse(wwdc._title_matches("", OFF_WEEK))


class EntryIdTest(unittest.TestCase):
    def test_prefers_the_link_then_falls_back_to_id(self):
        entry = SimpleNamespace(link="https://apple.com/a", id="tag:1")
        self.assertEqual(wwdc._entry_id(entry), "https://apple.com/a")
        self.assertEqual(wwdc._entry_id(SimpleNamespace(link="", id="tag:1")), "tag:1")
        self.assertEqual(wwdc._entry_id(SimpleNamespace()), "")


class BuildNotificationTest(unittest.TestCase):
    ENTRY = SimpleNamespace(title="WWDC26 keynote announced",
                            link="https://apple.com/wwdc",
                            summary="Apple shared the keynote date.")

    def test_ai_summary_becomes_the_body(self):
        with mock.patch.object(wwdc.summarize, "one_line", return_value="One-line take."):
            title, body, link = wwdc.build_notification(self.ENTRY)
        self.assertEqual(title, "Apple WWDC: WWDC26 keynote announced")
        self.assertEqual(body, "One-line take.")
        self.assertEqual(link, "https://apple.com/wwdc")

    def test_no_provider_falls_back_to_the_headline(self):
        with mock.patch.object(wwdc.summarize, "one_line", return_value=None):
            _, body, _ = wwdc.build_notification(self.ENTRY)
        self.assertEqual(body, "WWDC26 keynote announced")


if __name__ == "__main__":
    unittest.main()
