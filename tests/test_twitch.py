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


class HealthContractTest(unittest.TestCase):
    """run() reports its source outcome (notify_watcher.health)."""

    def _run(self, get_side_effect, state=None):
        with mock.patch.object(twitch.config, "section",
                               side_effect=lambda n: {"streamers": ["Sparg0"]}
                               if n == "twitch" else {}), \
                mock.patch.object(twitch, "_get", side_effect=get_side_effect):
            return twitch.run(state or {})

    def _status(self, state):
        from notify_watcher import health
        return (state.get(health.STATUS_KEY) or {}).get("twitch")

    def test_all_checks_failing_reports_source_failed(self):
        state = self._run(OSError("decapi down"))
        status = self._status(state)
        self.assertTrue(status["source_failed"])
        self.assertIn("decapi down", status["message"])

    def test_offline_answer_is_a_healthy_check(self):
        # "is offline" IS an answer from the source: ok, one check delivered.
        state = self._run(lambda kind, user: f"{user} is offline")
        status = self._status(state)
        self.assertTrue(status["ok"])
        self.assertEqual(status["data_count"], 1)

    def test_no_streamers_makes_no_claim(self):
        from notify_watcher import health
        with mock.patch.object(twitch.config, "section", return_value={}):
            state = twitch.run({})
        self.assertNotIn(health.STATUS_KEY, state)


if __name__ == "__main__":
    unittest.main()
