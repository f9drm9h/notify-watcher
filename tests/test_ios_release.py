"""Tests for the iOS-release watcher's pure helpers
(notify_watcher.topics.ios_release)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from notify_watcher.topics import ios_release


class IsWantedTest(unittest.TestCase):
    def test_stable_ios_and_ipados_releases_are_wanted(self):
        self.assertTrue(ios_release._is_wanted("iOS 18.5 (22F76)"))
        self.assertTrue(ios_release._is_wanted("iPadOS 18.5 (22F76)"))

    def test_other_platforms_are_not(self):
        self.assertFalse(ios_release._is_wanted("macOS 15.5 (24F74)"))
        self.assertFalse(ios_release._is_wanted("watchOS 11.5 (22T572)"))
        self.assertFalse(ios_release._is_wanted(""))

    def test_prerelease_builds_are_dropped(self):
        self.assertFalse(ios_release._is_wanted("iOS 26 beta 2 (23A5276f)"))
        self.assertFalse(ios_release._is_wanted("iOS 18.6 Release Candidate (22G80)"))


class EntryIdTest(unittest.TestCase):
    def test_title_carries_version_and_build_so_it_wins(self):
        entry = SimpleNamespace(title="iOS 18.5 (22F76)", link="https://x", id="y")
        self.assertEqual(ios_release._entry_id(entry), "iOS 18.5 (22F76)")

    def test_falls_back_to_link_then_id(self):
        self.assertEqual(
            ios_release._entry_id(SimpleNamespace(title="", link="https://x", id="y")),
            "https://x")
        self.assertEqual(
            ios_release._entry_id(SimpleNamespace(title="", link="", id="y")), "y")


class BodyTest(unittest.TestCase):
    def test_ai_take_when_available_else_the_title(self):
        with mock.patch.object(ios_release.summarize, "one_line",
                               return_value="Minor security fix; install soon."):
            self.assertEqual(ios_release._body("iOS 18.5 (22F76)"),
                             "Minor security fix; install soon.")
        with mock.patch.object(ios_release.summarize, "one_line", return_value=None):
            self.assertEqual(ios_release._body("iOS 18.5 (22F76)"), "iOS 18.5 (22F76)")


if __name__ == "__main__":
    unittest.main()
