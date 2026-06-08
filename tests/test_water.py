"""Tests for the daily drink-water reminder (notify_watcher.topics.water)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import water
from tests._util import capture_pushes


class MessageTest(unittest.TestCase):
    def test_message_is_deterministic_per_day(self):
        d = _dt.date(2026, 7, 20)
        self.assertEqual(water._message_for(d), water._message_for(d))

    def test_message_always_from_curated_set(self):
        # Sweep a full year; every pick must come from the vetted phrasings.
        for n in range(366):
            day = _dt.date(2026, 1, 1) + _dt.timedelta(days=n)
            self.assertIn(water._message_for(day), water._MESSAGES)

    def test_message_rotates_across_days(self):
        days = [_dt.date(2026, 1, 1) + _dt.timedelta(days=n) for n in range(7)]
        self.assertGreater(len({water._message_for(d) for d in days}), 1)


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_run_sends_one_push_and_stamps(self):
        with capture_pushes() as sent:
            state = water.run({})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Drink water")
        self.assertIn(sent[0]["message"], water._MESSAGES)
        self.assertEqual(state[water.STATE_KEY], _dt.date.today().isoformat())

    def test_run_is_idempotent_per_day(self):
        with capture_pushes() as sent:
            state = water.run({})
            water.run(state)  # second run same day
        self.assertEqual(len(sent), 1)

    def test_run_skips_when_not_daily(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = water.run({})
        self.assertEqual(sent, [])
        self.assertNotIn(water.STATE_KEY, state)


if __name__ == "__main__":
    unittest.main()
