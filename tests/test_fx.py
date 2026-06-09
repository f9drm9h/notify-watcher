"""Tests for the FX watcher (notify_watcher.topics.fx)."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from notify_watcher.topics import fx
from tests._util import capture_pushes

CFG = {"base": "USD", "quote": "DOP", "low": 57.0, "high": 61.0}


class ZoneTest(unittest.TestCase):
    def test_zones(self):
        self.assertEqual(fx._zone(55, 57, 61), "below")
        self.assertEqual(fx._zone(59, 57, 61), "within")
        self.assertEqual(fx._zone(62, 57, 61), "above")


class EvaluateTest(unittest.TestCase):
    def test_first_observation_seeds_silently(self):
        alert, zone, _ = fx._evaluate(59.0, CFG, None)
        self.assertFalse(alert)
        self.assertEqual(zone, "within")

    def test_no_alert_while_zone_unchanged(self):
        alert, *_ = fx._evaluate(58.0, CFG, "within")
        self.assertFalse(alert)

    def test_crossing_above_alerts(self):
        alert, zone, band = fx._evaluate(62.5, CFG, "within")
        self.assertTrue(alert)
        self.assertEqual(zone, "above")
        self.assertEqual(band, "above 61.00")

    def test_crossing_below_alerts(self):
        alert, zone, band = fx._evaluate(56.0, CFG, "within")
        self.assertTrue(alert)
        self.assertEqual(zone, "below")
        self.assertEqual(band, "below 57.00")

    def test_returning_to_band_alerts(self):
        alert, zone, band = fx._evaluate(59.0, CFG, "above")
        self.assertTrue(alert)
        self.assertEqual(zone, "within")
        self.assertIn("back in range", band)

    def test_missing_rate_is_silent(self):
        alert, *_ = fx._evaluate(None, CFG, "within")
        self.assertFalse(alert)


class WeeklyTrendTest(unittest.TestCase):
    """The trend goes through events.emit; with the real priority config the fx
    rule (45) routes it to the digest buffer, so we assert on state, not pushes."""

    MON = dt.date(2026, 6, 8)       # 2026-W24
    NEXT_MON = dt.date(2026, 6, 15)  # 2026-W25

    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _digest_titles(self, state):
        return [i.get("title") for i in state.get("digest_buffer", [])]

    def test_first_week_seeds_silently(self):
        with capture_pushes() as sent:
            state = fx._weekly_trend({}, 60.1, "USD", "DOP", today=self.MON)
        self.assertEqual(sent, [])
        self.assertEqual(state.get("digest_buffer", []), [])
        self.assertEqual(state[fx.WEEK_KEY], {"week": "2026-W24", "rate": 60.1})

    def test_new_week_digests_the_move_and_rebaselines(self):
        state = {fx.WEEK_KEY: {"week": "2026-W24", "rate": 60.1}}
        with capture_pushes():
            state = fx._weekly_trend(state, 60.8, "USD", "DOP", today=self.NEXT_MON)
        self.assertIn("USD/DOP weekly trend", self._digest_titles(state))
        detail = state["digest_buffer"][0]["detail"]
        self.assertIn("60.10", detail)
        self.assertIn("60.80", detail)
        self.assertIn("%", detail)
        self.assertEqual(state[fx.WEEK_KEY], {"week": "2026-W25", "rate": 60.8})

    def test_same_week_does_not_repeat(self):
        state = {fx.WEEK_KEY: {"week": "2026-W24", "rate": 60.1}}
        with capture_pushes():
            state = fx._weekly_trend(state, 60.8, "USD", "DOP", today=self.MON)
        self.assertEqual(state.get("digest_buffer", []), [])
        # Baseline untouched mid-week so the eventual summary spans the full week.
        self.assertEqual(state[fx.WEEK_KEY]["rate"], 60.1)

    def test_flat_week_still_reports(self):
        state = {fx.WEEK_KEY: {"week": "2026-W24", "rate": 60.1}}
        with capture_pushes():
            state = fx._weekly_trend(state, 60.1, "USD", "DOP", today=self.NEXT_MON)
        detail = state["digest_buffer"][0]["detail"]
        self.assertIn("held steady", detail)

    def test_not_daily_run_is_a_noop(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}):
            state = fx._weekly_trend({}, 60.1, "USD", "DOP", today=self.MON)
        self.assertNotIn(fx.WEEK_KEY, state)


if __name__ == "__main__":
    unittest.main()
