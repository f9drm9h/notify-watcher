"""Tests for the shared per-title news router (notify_watcher.news)."""
from __future__ import annotations

import unittest

from notify_watcher import digest, news
from tests._util import capture_pushes

# Synthetic scoring config: high >= 5, moderate >= 3.
SCORING = {
    "source_weights": {"official": 4, "tier1": 1, "unknown": 0},
    "source_tiers": {"official": ["rockstar"], "tier1": ["ign"]},
    "signal_bonuses": {
        "launch": {"weight": 5, "terms": ["release date"]},
        "moderate": {"weight": 3, "terms": ["interview", "leak"]},
    },
    "noise_penalties": {"listicle": {"weight": -6, "terms": ["top 10"]}},
    "thresholds": {"high": 5, "moderate": 3},
}
DIGEST = {"max_buffer": 50, "max_items_in_message": 25}


def _art(aid, headline, source=""):
    return (aid, headline, f"http://x/{aid}", source)


class SourceWeightKeyTest(unittest.TestCase):
    def test_maps_known_and_unknown(self):
        tiers = SCORING["source_tiers"]
        self.assertEqual(news._source_weight_key("Rockstar Games", tiers), "official")
        self.assertEqual(news._source_weight_key("IGN", tiers), "tier1")
        self.assertEqual(news._source_weight_key("Some Blog", tiers), "unknown")
        self.assertEqual(news._source_weight_key("", tiers), "unknown")


class RouteTest(unittest.TestCase):
    def _route(self, state, bucket, title, arts):
        news.route(
            state, bucket=bucket, title=title, articles=arts,
            scoring_cfg=SCORING, digest_cfg=DIGEST, cap=100,
            live_tag="video_game", live_title_prefix="Game news",
        )

    def test_first_run_seeds_silently(self):
        state, bucket = {}, {}
        with capture_pushes() as sent:
            self._route(state, bucket, "GameX", [_art("a", "release date set")])
        self.assertEqual(sent, [])  # no alerts on seed
        self.assertEqual(len(bucket["GameX"]), 1)

    def test_routes_each_tier_and_records_all_ids(self):
        state, bucket = {}, {"GameX": []}  # already seeded (empty)
        arts = [
            _art("live", "release date confirmed"),   # 5 -> live
            _art("dig", "developer interview"),        # 3 -> digest
            _art("drop", "top 10 games"),              # -6 -> dropped
        ]
        with capture_pushes() as sent:
            self._route(state, bucket, "GameX", arts)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"], "release date confirmed")
        self.assertEqual(sent[0]["priority"], "high")
        self.assertEqual(len(state.get(digest.BUFFER_KEY, [])), 1)
        # The dropped article must still be recorded as seen (the dedup fix).
        self.assertEqual(len(bucket["GameX"]), 3)

    def test_seen_articles_do_not_refire(self):
        state, bucket = {}, {"GameX": []}
        arts = [_art("live", "release date confirmed")]
        with capture_pushes() as sent:
            self._route(state, bucket, "GameX", arts)
            self._route(state, bucket, "GameX", arts)  # same batch again
        self.assertEqual(len(sent), 1)  # only fired once

    def test_official_source_can_elevate_to_live(self):
        # "interview"(3) + official source(4) = 7 -> live; same headline from an
        # unknown source stays in the digest.
        state, bucket = {}, {"GameX": []}
        with capture_pushes() as sent:
            self._route(state, bucket, "GameX", [_art("o", "developer interview", "Rockstar")])
        self.assertEqual(len(sent), 1)

        state2, bucket2 = {}, {"GameX": []}
        with capture_pushes() as sent2:
            self._route(state2, bucket2, "GameX", [_art("u", "developer interview", "")])
        self.assertEqual(sent2, [])
        self.assertEqual(len(state2.get(digest.BUFFER_KEY, [])), 1)


if __name__ == "__main__":
    unittest.main()
