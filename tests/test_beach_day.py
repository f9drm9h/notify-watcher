"""Tests for the beach day index (notify_watcher.topics.beach_day)."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from notify_watcher.topics import beach_day
from tests._util import capture_pushes

SATURDAY = dt.date(2026, 6, 13)
SUNDAY = dt.date(2026, 6, 14)


class ScoreTest(unittest.TestCase):
    def test_perfect_day(self):
        score, notes = beach_day._score(0.5, 10, 8, 31)
        self.assertEqual(score, 10)
        self.assertEqual(notes, [])

    def test_rough_seas_dominate(self):
        score, notes = beach_day._score(2.3, 10, 8, 31)
        self.assertEqual(score, 6)
        self.assertTrue(any("Rough seas" in n for n in notes))

    def test_rain_and_waves_stack(self):
        score, _ = beach_day._score(1.6, 80, 8, 31)
        self.assertEqual(score, 4)  # 10 - 2 (choppy) - 4 (rain)

    def test_extreme_uv_adds_caution(self):
        score, notes = beach_day._score(0.5, 10, 11.4, 31)
        self.assertEqual(score, 9)
        self.assertTrue(any("sunscreen" in n for n in notes))

    def test_cool_day_costs_one(self):
        score, _ = beach_day._score(0.5, 10, 8, 24)
        self.assertEqual(score, 9)

    def test_unknown_inputs_do_not_penalize(self):
        score, notes = beach_day._score(None, None, None, None)
        self.assertEqual(score, 10)
        self.assertEqual(notes, [])

    def test_floor_is_zero(self):
        score, _ = beach_day._score(3.0, 95, 12, 20)
        self.assertEqual(score, 0)


class ComposeTest(unittest.TestCase):
    def test_full_data_renders_facts_line(self):
        title, body = beach_day._compose(0.7, 15, 9, 31)
        self.assertEqual(title, "Beach day: 10/10 - great day for the beach")
        self.assertIn("Waves 0.7 m | Rain 15% | UV 9 | 31 C", body)

    def test_missing_inputs_are_marked_unknown(self):
        _, body = beach_day._compose(None, 15, 9, 31)
        self.assertIn("Waves unknown", body)

    def test_verdict_bands(self):
        self.assertIn("great", beach_day._verdict(8))
        self.assertIn("good", beach_day._verdict(6))
        self.assertIn("iffy", beach_day._verdict(4))
        self.assertIn("skip", beach_day._verdict(3))


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run(self, state, day, wave=0.7, forecast=None):
        forecast = forecast if forecast is not None else {"precip": 15.0, "uv": 9.0, "temp": 31.0}
        with mock.patch.object(beach_day, "_today", return_value=day), \
             mock.patch.object(beach_day, "_fetch_marine", return_value=wave), \
             mock.patch.object(beach_day, "_fetch_forecast", return_value=forecast), \
             capture_pushes() as sent:
            state = beach_day.run(state)
        return state, sent

    def test_saturday_sends_once(self):
        state, sent = self._run({}, SATURDAY)
        self.assertEqual(len(sent), 1)
        self.assertIn("Beach day:", sent[0]["title"])
        state, sent2 = self._run(state, SATURDAY)
        self.assertEqual(sent2, [])  # same-day dedup

    def test_other_weekdays_are_silent(self):
        _, sent = self._run({}, SUNDAY)
        self.assertEqual(sent, [])

    def test_partial_outage_still_scores(self):
        with mock.patch.object(beach_day, "_today", return_value=SATURDAY), \
             mock.patch.object(beach_day, "_fetch_marine", side_effect=RuntimeError("x")), \
             mock.patch.object(beach_day, "_fetch_forecast",
                               return_value={"precip": 15.0, "uv": 9.0, "temp": 31.0}), \
             capture_pushes() as sent:
            beach_day.run({})
        self.assertEqual(len(sent), 1)
        self.assertIn("Waves unknown", sent[0]["message"])

    def test_total_outage_skips_without_stamp(self):
        with mock.patch.object(beach_day, "_today", return_value=SATURDAY), \
             mock.patch.object(beach_day, "_fetch_marine", side_effect=RuntimeError("x")), \
             mock.patch.object(beach_day, "_fetch_forecast", side_effect=RuntimeError("x")), \
             capture_pushes() as sent:
            state = beach_day.run({})
        self.assertEqual(sent, [])
        self.assertNotIn(beach_day.STATE_KEY, state)

    def test_not_daily_run_is_a_noop(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}), \
             capture_pushes() as sent:
            beach_day.run({})
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
