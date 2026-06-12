"""Tests for the earthquake watcher (notify_watcher.topics.quakes)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import ids
from notify_watcher.topics import quakes
from tests._util import capture_pushes

# Home ~ Santo Domingo Este.
HOME = (18.521661, -69.8224191)
CFG = {
    "live_radius_km": 600, "live_min_mag": 4.5,
    "digest_radius_km": 300, "digest_min_mag": 3.0,
}


def _feature(fid, mag, lat, lon, place="somewhere", depth=10):
    return {"id": fid, "properties": {"mag": mag, "place": place,
            "url": f"http://usgs/{fid}"}, "geometry": {"coordinates": [lon, lat, depth]}}


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

    def test_tsunami_quake_included_beyond_normal_range(self):
        # Big shallow quake ~1000 km north of SDE: beyond live_radius (600) but
        # within tsunami_radius (1500) -> included with the tsunami flag set.
        feats = [_feature("tsu", 7.5, 27.5, -69.82, "offshore", depth=10)]
        rows = quakes._evaluate(feats, HOME, CFG)
        self.assertEqual(len(rows), 1)
        fid, tier, mag, dist, place, url, tsunami = rows[0]
        self.assertTrue(tsunami)
        self.assertIsNone(tier)          # outside the nearby live/digest radius
        self.assertTrue(600 < dist < 1500)


class TsunamiRiskTest(unittest.TestCase):
    def test_big_shallow_near_is_risk(self):
        self.assertTrue(quakes._tsunami_risk(7.2, 20, 800, CFG))

    def test_big_but_deep_is_not(self):
        self.assertFalse(quakes._tsunami_risk(7.2, 300, 800, CFG))

    def test_big_but_too_far_is_not(self):
        self.assertFalse(quakes._tsunami_risk(7.2, 20, 4000, CFG))

    def test_moderate_magnitude_is_not(self):
        self.assertFalse(quakes._tsunami_risk(6.0, 10, 100, CFG))


class FirstRunSeedingTest(unittest.TestCase):
    """The seeding fix: a first run records only acted-on (evaluated) ids, NOT
    every event in the feed, so a quake currently too small/far to alert stays
    unseen and can still alert if a later feed revises it into range."""

    def _run_first(self, feats):
        resp = mock.Mock()
        resp.json.return_value = {"features": feats}
        resp.raise_for_status.return_value = None
        sections = {"location": {"latitude": HOME[0], "longitude": HOME[1]}, "quakes": CFG}
        with mock.patch.object(quakes.requests, "get", return_value=resp), \
             mock.patch.object(quakes.config, "section", side_effect=lambda n: sections.get(n, {})), \
             capture_pushes() as sent:
            state = quakes.run({})
        return state, sent

    def test_seeds_only_acted_on_ids(self):
        feats = [
            _feature("near_big", 5.2, 18.7, -69.9, "near SDE"),     # acted-on (live)
            _feature("near_tiny", 2.5, 18.6, -69.8, "right here"),  # dropped, unseen
        ]
        state, sent = self._run_first(feats)
        self.assertEqual(sent, [])  # first run never alerts
        self.assertEqual(state["quake_seen_ids"], [ids.short("near_big")])
        # The tiny quake is deliberately left unseen so it can alert later.
        self.assertNotIn(ids.short("near_tiny"), state["quake_seen_ids"])

    def test_seeds_empty_when_nothing_actionable(self):
        feats = [_feature("far_tiny", 2.6, 35.0, 139.0, "Japan")]  # far + small
        state, sent = self._run_first(feats)
        self.assertEqual(sent, [])
        self.assertEqual(state["quake_seen_ids"], [])


class SteadyStateRoutingTest(unittest.TestCase):
    """Post-seeding, routing now flows through events.emit; with no `priority`
    section the engine is OFF and behavior is unchanged: a live quake pushes,
    a moderate one buffers to the digest."""

    def _run(self, feats, state):
        resp = mock.Mock()
        resp.json.return_value = {"features": feats}
        resp.raise_for_status.return_value = None
        sections = {"location": {"latitude": HOME[0], "longitude": HOME[1]},
                    "quakes": CFG, "digest": {}, "priority": {}}
        with mock.patch.object(quakes.requests, "get", return_value=resp), \
             mock.patch.object(quakes.config, "section",
                               side_effect=lambda n: sections.get(n, {})), \
             capture_pushes() as sent:
            state = quakes.run(state)
        return state, sent

    def test_live_quake_pushes(self):
        feats = [_feature("near_big", 5.2, 18.7, -69.9, "near SDE")]
        _, sent = self._run(feats, {"quake_seen_ids": []})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Earthquake nearby")
        self.assertEqual(sent[0]["priority"], "high")  # 4.5 <= mag < 6 -> high

    def test_moderate_quake_digests_not_pushes(self):
        from notify_watcher import digest
        feats = [_feature("near_mid", 3.4, 19.0, -70.0, "Santiago")]
        state, sent = self._run(feats, {"quake_seen_ids": []})
        self.assertEqual(sent, [])
        self.assertEqual(len(state.get(digest.BUFFER_KEY, [])), 1)
        self.assertEqual(state[digest.BUFFER_KEY][0]["source"], "Earthquakes")


class HealthContractTest(unittest.TestCase):
    """run() reports its source outcome (notify_watcher.health)."""

    SECTIONS = {"location": {"latitude": HOME[0], "longitude": HOME[1]},
                "quakes": CFG}

    def test_fetch_failure_reports_source_failed(self):
        from notify_watcher import health
        with mock.patch.object(quakes.requests, "get",
                               side_effect=OSError("connection refused")), \
                mock.patch.object(quakes.config, "section",
                                  side_effect=lambda n: self.SECTIONS.get(n, {})):
            state = quakes.run({})
        status = state[health.STATUS_KEY]["quakes"]
        self.assertTrue(status["source_failed"])
        self.assertIn("USGS fetch failed", status["message"])

    def test_feed_reports_ok_with_prefilter_feature_count(self):
        from notify_watcher import health
        resp = mock.Mock()
        resp.json.return_value = {"features": [
            _feature("far_tiny", 2.6, 35.0, 139.0, "Japan")]}
        resp.raise_for_status.return_value = None
        with mock.patch.object(quakes.requests, "get", return_value=resp), \
                mock.patch.object(quakes.config, "section",
                                  side_effect=lambda n: self.SECTIONS.get(n, {})), \
                capture_pushes():
            state = quakes.run({})
        status = state[health.STATUS_KEY]["quakes"]
        self.assertTrue(status["ok"])
        self.assertEqual(status["data_count"], 1)

    def test_no_location_makes_no_claim(self):
        from notify_watcher import health
        with mock.patch.object(quakes.config, "section", return_value={}):
            state = quakes.run({})
        self.assertNotIn(health.STATUS_KEY, state)


if __name__ == "__main__":
    unittest.main()
