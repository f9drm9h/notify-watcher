"""Tests for the air-quality watcher (notify_watcher.topics.air_quality)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import air_quality as aq

TODAY = "2026-06-08"
ALERT_INDEX = 2  # Unhealthy for sensitive groups


class BandTest(unittest.TestCase):
    def test_band_boundaries(self):
        self.assertEqual(aq._band(40)[1], "Good")
        self.assertEqual(aq._band(72)[1], "Moderate")
        self.assertEqual(aq._band(120)[1], "Unhealthy for sensitive groups")
        self.assertEqual(aq._band(160)[1], "Unhealthy")
        self.assertEqual(aq._band(350)[1], "Hazardous")


class ShouldAlertTest(unittest.TestCase):
    def test_below_threshold_is_silent(self):
        alert, *_ = aq._should_alert(72, {}, TODAY, ALERT_INDEX)
        self.assertFalse(alert)

    def test_first_crossing_alerts(self):
        alert, idx, label, _ = aq._should_alert(130, {}, TODAY, ALERT_INDEX)
        self.assertTrue(alert)
        self.assertEqual(idx, 2)

    def test_same_band_same_day_is_silent(self):
        prev = {"date": TODAY, "band": 2}
        alert, *_ = aq._should_alert(135, prev, TODAY, ALERT_INDEX)
        self.assertFalse(alert)

    def test_worsening_band_same_day_alerts(self):
        prev = {"date": TODAY, "band": 2}
        alert, idx, *_ = aq._should_alert(175, prev, TODAY, ALERT_INDEX)  # -> band 3
        self.assertTrue(alert)
        self.assertEqual(idx, 3)

    def test_new_day_resets(self):
        prev = {"date": "2026-06-07", "band": 3}
        alert, *_ = aq._should_alert(130, prev, TODAY, ALERT_INDEX)
        self.assertTrue(alert)

    def test_missing_aqi_is_silent(self):
        alert, *_ = aq._should_alert(None, {}, TODAY, ALERT_INDEX)
        self.assertFalse(alert)


if __name__ == "__main__":
    unittest.main()
