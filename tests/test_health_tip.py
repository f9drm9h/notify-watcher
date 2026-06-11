"""Tests for the daily health tip (notify_watcher.topics.health_tip).

The tip text comes from the vetted KB (data/health_tips.json, read for real —
no network); the optional AI rewording is patched out so the verbatim-fallback
path is what's under test.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import health_tip
from tests._util import capture_pushes


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        # No provider key in tests: send the vetted tip verbatim.
        self._ai = mock.patch.object(health_tip.summarize, "one_line",
                                     return_value=None)
        self._ai.start()
        self.addCleanup(self._ai.stop)

    def test_sends_one_tip_and_stamps_the_day(self):
        with capture_pushes() as sent:
            state = health_tip.run({})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Health tip")
        self.assertTrue(sent[0]["message"])  # a real tip from the KB
        self.assertEqual(state[health_tip.STATE_KEY], health_tip._today())

    def test_same_day_rerun_does_not_double_send(self):
        state = {health_tip.STATE_KEY: health_tip._today()}
        with capture_pushes() as sent:
            health_tip.run(state)
        self.assertEqual(sent, [])

    def test_skips_outside_the_daily_run(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}), \
             capture_pushes() as sent:
            state = health_tip.run({})
        self.assertEqual(sent, [])
        self.assertEqual(state, {})


if __name__ == "__main__":
    unittest.main()
