"""Tests for the FX watcher (notify_watcher.topics.fx)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import fx

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


if __name__ == "__main__":
    unittest.main()
