"""Tests for the Twitch live watcher (notify_watcher.topics.twitch)."""
from __future__ import annotations

import unittest
from unittest import mock

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


class FollowsMergeTest(unittest.TestCase):
    def test_followed_streamers_are_checked_too(self):
        # [Watch streamer] overlay entries join the config list (dedup,
        # case-insensitive), so a button follow is watched without a config edit.
        state = {"follows": {"streamers": [{"name": "newstreamer"},
                                           {"name": "SPARG0"}]}}
        checked: list[str] = []

        def fake_get(kind, user):
            checked.append(user)
            return f"{user} is offline"

        with mock.patch.object(twitch.config, "section",
                               side_effect=lambda n: {"streamers": ["Sparg0"]}
                               if n == "twitch" else {}), \
                mock.patch.object(twitch, "_get", side_effect=fake_get):
            twitch.run(state)
        self.assertEqual(checked, ["Sparg0", "newstreamer"])


if __name__ == "__main__":
    unittest.main()
