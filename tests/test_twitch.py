"""Tests for the Twitch live watcher (notify_watcher.topics.twitch)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import twitch


class IsLiveTest(unittest.TestCase):
    def test_uptime_string_is_live(self):
        self.assertTrue(twitch._is_live("2 hours, 13 minutes"))
        self.assertTrue(twitch._is_live("just now"))

    def test_offline_is_not_live(self):
        self.assertFalse(twitch._is_live("Sparg0 is offline"))

    def test_unknown_user_is_not_live(self):
        self.assertFalse(twitch._is_live("User not found"))
        self.assertFalse(twitch._is_live("Unknown user: foo"))

    def test_blank_or_error_is_not_live(self):
        self.assertFalse(twitch._is_live(""))
        self.assertFalse(twitch._is_live("  "))
        self.assertFalse(twitch._is_live("Error from Twitch API"))


if __name__ == "__main__":
    unittest.main()
