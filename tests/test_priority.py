"""Tests for the cross-topic Personal Priority Engine (notify_watcher.priority).

Pure scorer: no network, no state. A small synthetic config keeps these tests
independent of monitors.json, mirroring tests/test_scoring.py.
"""
from __future__ import annotations

import unittest

from notify_watcher import priority
from notify_watcher.events import Event

# The locked defaults, plus the requirement's worked example as rules.
CFG = {
    "threshold": 60,
    "digest_floor": 25,
    "default": 30,
    "urgency_bands": {"90": "urgent", "70": "high", "0": "default"},
    "rules": [
        {"topic": "visa_bulletin", "score": 100},
        {"topic": "weather", "severity": "critical", "score": 95},
        {"topic": "fda", "score": 70},
        {"topic": "ios_release", "score": 40},
        {"topic": "movies", "score": 15},
    ],
    "keyword_boosts": [{"terms": ["hurricane", "tsunami"], "add": 20}],
    "source_boosts": [{"source": "NHC", "add": 10}],
    "overrides": [{"topic": "movies", "source": "A24", "set": 55}],
}


def ev(topic="movies", severity="moderate", source="", title="", body=""):
    return Event(
        title=title, body=body, topic=topic, severity=severity,
        source=source, timestamp="2026-01-01T00:00:00+00:00", metadata={},
    )


class EngineOffTest(unittest.TestCase):
    def test_empty_config_is_engine_off(self):
        self.assertIsNone(priority.decide(ev(), {}))

    def test_non_dict_config_is_engine_off(self):
        self.assertIsNone(priority.decide(ev(), None))
        self.assertIsNone(priority.decide(ev(), []))


class WorkedExampleTest(unittest.TestCase):
    """The exact routings promised in the design for the locked thresholds."""

    def test_visa_pushes_urgent(self):
        d = priority.decide(ev(topic="visa_bulletin"), CFG)
        self.assertEqual((d.action, d.score, d.ntfy_priority), ("push", 100, "urgent"))

    def test_hurricane_warning_pushes_urgent(self):
        d = priority.decide(ev(topic="weather", severity="critical"), CFG)
        self.assertEqual((d.action, d.score, d.ntfy_priority), ("push", 95, "urgent"))

    def test_drug_approval_pushes_high(self):
        d = priority.decide(ev(topic="fda"), CFG)
        self.assertEqual((d.action, d.score, d.ntfy_priority), ("push", 70, "high"))

    def test_ios_release_digests(self):
        d = priority.decide(ev(topic="ios_release"), CFG)
        self.assertEqual((d.action, d.score, d.ntfy_priority), ("digest", 40, None))

    def test_movie_change_drops(self):
        d = priority.decide(ev(topic="movies"), CFG)
        self.assertEqual((d.action, d.score, d.ntfy_priority), ("drop", 15, None))


class RuleSelectionTest(unittest.TestCase):
    def test_unmatched_topic_uses_default(self):
        d = priority.decide(ev(topic="unknown_topic"), CFG)
        self.assertEqual(d.score, 30)  # cfg["default"]
        self.assertEqual(d.action, "digest")  # 25 <= 30 < 60

    def test_severity_narrowed_rule_does_not_match_other_severity(self):
        # weather rule requires severity=critical; a moderate weather event falls
        # through to the default rather than scoring 95.
        d = priority.decide(ev(topic="weather", severity="moderate"), CFG)
        self.assertEqual(d.score, 30)
        self.assertEqual(d.action, "digest")

    def test_first_matching_rule_wins(self):
        cfg = {
            "threshold": 60, "digest_floor": 25, "default": 30,
            "rules": [
                {"topic": "movies", "score": 80},   # first -> wins
                {"topic": "movies", "score": 10},
            ],
        }
        self.assertEqual(priority.decide(ev(topic="movies"), cfg).score, 80)


class BoostTest(unittest.TestCase):
    def test_keyword_boost_group_counts_once(self):
        # ios base 40 + keyword group (+20 once, even with BOTH terms) = 60 -> push.
        d = priority.decide(
            ev(topic="ios_release", body="hurricane and tsunami incoming"), CFG)
        self.assertEqual(d.score, 60)
        self.assertEqual(d.action, "push")

    def test_source_boost_substring_match(self):
        # movies base 15 + source "NHC" substring in "NHC Atlantic" (+10) = 25 ->
        # exactly the digest floor.
        d = priority.decide(ev(topic="movies", source="NHC Atlantic"), CFG)
        self.assertEqual(d.score, 25)
        self.assertEqual(d.action, "digest")

    def test_severity_boost_applies(self):
        cfg = {
            "threshold": 60, "digest_floor": 25, "default": 30,
            "rules": [{"topic": "fda", "score": 55}],
            "severity_boosts": [{"severity": "critical", "add": 10}],
        }
        self.assertEqual(
            priority.decide(ev(topic="fda", severity="critical"), cfg).score, 65)


class OverrideTest(unittest.TestCase):
    def test_override_clamps_final_score(self):
        # movies+A24 normally scores 15 (drop); the override sets it to 55 (digest).
        d = priority.decide(ev(topic="movies", source="A24"), CFG)
        self.assertEqual(d.score, 55)
        self.assertEqual(d.action, "digest")

    def test_override_only_for_matching_source(self):
        d = priority.decide(ev(topic="movies", source="Disney"), CFG)
        self.assertEqual(d.score, 15)  # override does not apply


class BandingTest(unittest.TestCase):
    def test_threshold_boundary_pushes(self):
        cfg = {"threshold": 60, "digest_floor": 25, "default": 60}
        self.assertEqual(priority.decide(ev(), cfg).action, "push")  # 60 >= 60

    def test_floor_boundary_digests(self):
        cfg = {"threshold": 60, "digest_floor": 25, "default": 25}
        self.assertEqual(priority.decide(ev(), cfg).action, "digest")  # 25 >= 25

    def test_below_floor_drops(self):
        cfg = {"threshold": 60, "digest_floor": 25, "default": 24}
        self.assertEqual(priority.decide(ev(), cfg).action, "drop")

    def test_ntfy_band_selection(self):
        self.assertEqual(priority._ntfy_priority(95, CFG["urgency_bands"]), "urgent")
        self.assertEqual(priority._ntfy_priority(70, CFG["urgency_bands"]), "high")
        self.assertEqual(priority._ntfy_priority(65, CFG["urgency_bands"]), "default")


class FailSoftTest(unittest.TestCase):
    def test_malformed_knobs_fall_back_to_defaults_without_raising(self):
        cfg = {"threshold": "oops", "digest_floor": None, "default": "x",
               "rules": "not-a-list", "keyword_boosts": [42, {"bad": True}]}
        d = priority.decide(ev(topic="fda"), cfg)
        # threshold->60, floor->25, default->30, rules ignored -> score 30 -> digest.
        self.assertEqual((d.action, d.score), ("digest", 30))


class ShippedConfigTest(unittest.TestCase):
    """Lock the LIVE monitors.json priority section to the worked example, so a
    future edit that breaks the engine's routing is caught in CI."""

    @classmethod
    def setUpClass(cls):
        from notify_watcher import config
        cls.cfg = config.section("priority")

    def _d(self, **kw):
        return priority.decide(ev(**kw), self.cfg)

    def test_engine_is_on(self):
        self.assertTrue(self.cfg, "monitors.json must ship a `priority` section")

    def test_worked_example_anchors(self):
        self.assertEqual(self._d(topic="visa_bulletin").ntfy_priority, "urgent")
        self.assertEqual(self._d(topic="weather", severity="critical").action, "push")
        self.assertEqual(self._d(topic="quakes", severity="critical").ntfy_priority, "urgent")
        self.assertEqual(self._d(topic="fda", severity="high").action, "push")
        self.assertEqual(self._d(topic="ios_release").action, "digest")
        self.assertEqual(self._d(topic="movies", severity="low").action, "drop")

    def test_heads_up_topics_digest(self):
        for t in ("holidays", "blood_donation", "fx", "uv", "marine"):
            self.assertEqual(self._d(topic=t).action, "digest", t)

    def test_safety_rings_through_quiet_hours(self):
        # urgent/high bypass quiet hours; default does not.
        self.assertIn(self._d(topic="quakes", severity="high").ntfy_priority, ("high", "urgent"))
        self.assertEqual(self._d(topic="twitch").ntfy_priority, "default")  # pushes, quiet-respecting


if __name__ == "__main__":
    unittest.main()
