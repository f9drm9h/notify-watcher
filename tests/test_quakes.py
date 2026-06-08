"""Tests for the earthquake watcher (notify_watcher.topics.quakes)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import quakes

# Home ~ Santo Domingo Este.
HOME = (18.521661, -69.8224191)
CFG = {
    "live_radius_km": 600, "live_min_mag": 4.5,
    "digest_radius_km": 300, "digest_min_mag": 3.0,
}


def _feature(fid, mag, lat, lon, place="somewhere"):
    return {"id": fid, "properties": {"mag": mag, "place": place,
            "url": f"http://usgs/{fid}"}, "geometry": {"coordinates": [lon, lat, 10]}}


class HaversineTest(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(quakes._haversine_km(*HOME, *HOME), 0.0, places=3)

    def test_known_distance(self):
        # SDE -> Santiago, DR (~19.45, -70.7) is roughly 150 km.
        d = quakes._haversine_km(18.5217, -69.8224, 19.45, -70.70)
        self.assertTrue(120 < d < 180, d)


class ClassifyTest(unittest.TestCase):
    def test_strong_and_close_is_live(self):
        self.assertEqual(quakes._classify(5.0, 100, CFG), "live")

    def test_moderate_and_near_is_digest(self):
        self.assertEqual(quakes._classify(3.5, 200, CFG), "digest")

    def test_strong_but_far_is_dropped(self):
        self.assertIsNone(quakes._classify(5.0, 5000, CFG))

    def test_tiny_nearby_is_dropped(self):
        self.assertIsNone(quakes._classify(2.6, 50, CFG))

    def test_missing_magnitude_is_dropped(self):
        self.assertIsNone(quakes._classify(None, 10, CFG))


class EvaluateTest(unittest.TestCase):
    def test_routes_by_distance_and_magnitude(self):
        feats = [
            _feature("near_big", 5.2, 18.7, -69.9, "near SDE"),     # live
            _feature("near_mid", 3.4, 19.0, -70.0, "Santiago"),     # digest
            _feature("far_big", 7.0, 35.0, 139.0, "Japan"),         # dropped
            _feature("near_tiny", 2.5, 18.6, -69.8, "right here"),  # dropped
        ]
        got = {row[0]: row[1] for row in quakes._evaluate(feats, HOME, CFG)}
        self.assertEqual(got, {"near_big": "live", "near_mid": "digest"})

    def test_skips_malformed_features(self):
        feats = [{"id": "x", "properties": {"mag": 5.0}, "geometry": {}},  # no coords
                 {"properties": {"mag": 5.0}, "geometry": {"coordinates": [0, 0]}}]  # no id
        self.assertEqual(quakes._evaluate(feats, HOME, CFG), [])


if __name__ == "__main__":
    unittest.main()
