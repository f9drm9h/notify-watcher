"""Tests for the weekly gating of the games topic (notify_watcher.topics.games)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import games


class IsoWeekTest(unittest.TestCase):
    def test_label_format(self):
        self.assertEqual(games._iso_week(_dt.date(2026, 6, 8)), "2026-W24")

    def test_same_week_same_label(self):
        mon = _dt.date(2026, 6, 8)
        sun = _dt.date(2026, 6, 14)  # same ISO week
        self.assertEqual(games._iso_week(mon), games._iso_week(sun))

    def test_next_week_differs(self):
        self.assertNotEqual(
            games._iso_week(_dt.date(2026, 6, 8)),
            games._iso_week(_dt.date(2026, 6, 15)),
        )


class RunGateTest(unittest.TestCase):
    def test_skips_when_not_daily(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             mock.patch.object(games, "_track_release_dates") as rd, \
             mock.patch.object(games, "_track_news") as nw:
            state = games.run({})
        rd.assert_not_called()
        nw.assert_not_called()
        self.assertNotIn(games.WEEK_STATE_KEY, state)

    def test_runs_once_per_week_and_stamps(self):
        week = games._iso_week(_dt.date.today())
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}, clear=False), \
             mock.patch.object(games, "_track_release_dates", side_effect=lambda s: s) as rd, \
             mock.patch.object(games, "_track_news", side_effect=lambda s: s) as nw:
            state = games.run({})
            self.assertEqual(state[games.WEEK_STATE_KEY], week)
            self.assertEqual(rd.call_count, 1)
            # Second run the same week is a no-op.
            games.run(state)
            self.assertEqual(rd.call_count, 1)
            self.assertEqual(nw.call_count, 1)

    def test_runs_again_when_week_rolls_over(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}, clear=False), \
             mock.patch.object(games, "_track_release_dates", side_effect=lambda s: s) as rd, \
             mock.patch.object(games, "_track_news", side_effect=lambda s: s):
            state = {games.WEEK_STATE_KEY: "2000-W01"}  # stale week
            games.run(state)
        rd.assert_called_once()


if __name__ == "__main__":
    unittest.main()
