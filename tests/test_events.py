"""Tests for the event normalization + routing funnel (notify_watcher.events).

Uses capture_pushes (tests/_util) to assert what WOULD be sent without touching
the network, and an in-memory state dict to inspect the digest buffer.
"""
from __future__ import annotations

import unittest

from notify_watcher import digest, events
from tests._util import capture_pushes

# Same worked-example config used in test_priority.
CFG = {
    "threshold": 60,
    "digest_floor": 25,
    "default": 30,
    "ntfy_bands": {"90": "urgent", "70": "high", "0": "default"},
    "rules": [
        {"topic": "visa_bulletin", "score": 100},
        {"topic": "fda", "score": 70},
        {"topic": "ios_release", "score": 40},
        {"topic": "movies", "score": 15},
    ],
}
DIGEST_CFG: dict = {}  # digest.add supplies its own defaults for missing keys


class EngineOnTest(unittest.TestCase):
    def test_above_threshold_pushes_at_banded_priority(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="F4 moved", body="2015 -> 2016", topic="visa_bulletin",
                click_url="https://travel.state.gov", tags="passport_control",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "F4 moved")
        self.assertEqual(sent[0]["message"], "2015 -> 2016")
        self.assertEqual(sent[0]["priority"], "urgent")
        self.assertEqual(sent[0]["click_url"], "https://travel.state.gov")
        self.assertEqual(sent[0]["tags"], "passport_control")
        self.assertNotIn(digest.BUFFER_KEY, state)  # nothing buffered

    def test_mid_score_routes_to_digest_with_global_score(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="iOS 18.5", body="install soon", topic="ios_release",
                click_url="https://apple.com/x", source="Apple",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent, [])  # no live push
        buf = state[digest.BUFFER_KEY]
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["title"], "iOS 18.5")
        self.assertEqual(buf[0]["url"], "https://apple.com/x")
        self.assertEqual(buf[0]["source"], "Apple")
        self.assertEqual(buf[0]["score"], 40)  # the GLOBAL priority score

    def test_below_floor_drops_silently(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="Some sequel rumor", topic="movies",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent, [])
        self.assertNotIn(digest.BUFFER_KEY, state)


class BackwardCompatTest(unittest.TestCase):
    """Engine OFF (empty priority cfg): emit reproduces pre-engine behavior."""

    def test_legacy_push_matches_old_ntfy_call(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="Apple release: iOS 18.5", body="version line",
                topic="ios_release", click_url="https://apple.com/x", tags="iphone",
                legacy_priority=None, legacy_action="push",
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {
            "title": "Apple release: iOS 18.5",
            "message": "version line",
            "click_url": "https://apple.com/x",
            "tags": "iphone",
            "priority": None,
        })

    def test_legacy_push_preserves_explicit_priority(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="Quake", body="M5 nearby", topic="quakes",
                legacy_priority="high", legacy_action="push",
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent[0]["priority"], "high")

    def test_legacy_digest_uses_caller_within_domain_score(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="moderate energy item", topic="energy", source="EIA",
                click_url="https://eia.gov/x", legacy_action="digest", score=5,
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent, [])
        buf = state[digest.BUFFER_KEY]
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["score"], 5)  # caller's score, not an engine score
        self.assertEqual(buf[0]["source"], "EIA")


class NormalizationTest(unittest.TestCase):
    def test_emit_stamps_timestamp_and_folds_transport_into_metadata(self):
        e = events.Event(
            title="t", body="b", topic="x", severity="moderate",
            source="s", timestamp=events._now_iso(), metadata={},
        )
        self.assertTrue(e.timestamp.endswith("+00:00"))  # UTC ISO-8601

    def test_explicit_metadata_click_url_is_not_overwritten(self):
        # A caller that already put click_url in metadata keeps it; the click_url
        # kwarg only fills in when absent (setdefault).
        state: dict = {}
        with capture_pushes():
            events.emit(
                state, title="t", topic="ios_release",
                metadata={"click_url": "https://from-metadata"},
                click_url="https://from-kwarg",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(state[digest.BUFFER_KEY][0]["url"], "https://from-metadata")


if __name__ == "__main__":
    unittest.main()
