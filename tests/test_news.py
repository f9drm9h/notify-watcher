"""Tests for the shared per-title news router (notify_watcher.news)."""
from __future__ import annotations

import unittest
from unittest import mock

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


class IsRecentTest(unittest.TestCase):
    """The freshness gate that stops Google News resurfacing old articles."""

    NOW = 1_770_000_000.0  # fixed epoch so the tests are deterministic

    def _entry(self, days_ago: float):
        import time as _time
        import types
        return types.SimpleNamespace(
            published_parsed=_time.gmtime(self.NOW - days_ago * 86400))

    def test_recent_entry_passes(self):
        self.assertTrue(news.is_recent(self._entry(3), 14, now=self.NOW))

    def test_old_entry_is_gated(self):
        self.assertFalse(news.is_recent(self._entry(60), 14, now=self.NOW))
        self.assertFalse(news.is_recent(self._entry(400), 14, now=self.NOW))

    def test_boundary_day_still_passes(self):
        self.assertTrue(news.is_recent(self._entry(14), 14, now=self.NOW))

    def test_undated_entry_passes(self):
        import types
        self.assertTrue(news.is_recent(types.SimpleNamespace(), 14, now=self.NOW))

    def test_zero_or_bad_window_disables_the_gate(self):
        self.assertTrue(news.is_recent(self._entry(400), 0, now=self.NOW))
        self.assertTrue(news.is_recent(self._entry(400), None, now=self.NOW))
        self.assertTrue(news.is_recent(self._entry(400), "x", now=self.NOW))

    def test_garbage_date_passes(self):
        import types
        e = types.SimpleNamespace(published_parsed="not-a-struct-time")
        self.assertTrue(news.is_recent(e, 14, now=self.NOW))


class SourceWeightKeyTest(unittest.TestCase):
    def test_maps_known_and_unknown(self):
        tiers = SCORING["source_tiers"]
        self.assertEqual(news._source_weight_key("Rockstar Games", tiers), "official")
        self.assertEqual(news._source_weight_key("IGN", tiers), "tier1")
        self.assertEqual(news._source_weight_key("Some Blog", tiers), "unknown")
        self.assertEqual(news._source_weight_key("", tiers), "unknown")


class RouteTest(unittest.TestCase):
    def setUp(self):
        # Pin the Personal Priority Engine OFF so these tests exercise route's
        # LEGACY tier routing; monitors.json now ships a `priority` section that
        # emit would otherwise read (via config.section) and apply.
        p = mock.patch("notify_watcher.config.section", return_value={})
        p.start()
        self.addCleanup(p.stop)

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

    def test_legacy_raw_id_in_bucket_migrates_without_refiring(self):
        # Pre-migration bucket holds the raw article id; the same article must
        # be recognised by its hash and not re-pushed.
        from notify_watcher import ids
        state, bucket = {}, {"GameX": ["live"]}  # legacy raw id
        with capture_pushes() as sent:
            self._route(state, bucket, "GameX", [_art("live", "release date confirmed")])
        self.assertEqual(sent, [])
        self.assertEqual(bucket["GameX"], [ids.short("live")])  # now stored hashed

    def test_live_push_label_and_digest_grouping_preserved(self):
        # The push Title is "<prefix>: <game/film title>" and the digest groups
        # by that title (not the publisher) — preserved across the emit migration.
        state, bucket = {}, {"GameX": []}
        with capture_pushes() as sent:
            self._route(state, bucket, "GameX",
                        [_art("live", "release date confirmed", "IGN"),
                         _art("dig", "developer interview", "IGN")])
        self.assertEqual(sent[0]["title"], "Game news: GameX")
        self.assertEqual(sent[0]["tags"], "video_game")
        self.assertEqual(state[digest.BUFFER_KEY][0]["source"], "GameX")
        self.assertEqual(state[digest.BUFFER_KEY][0]["title"], "developer interview")

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


class SyndicationDedupTest(unittest.TestCase):
    """One story republished across regional sites must route exactly once."""

    def setUp(self):
        p = mock.patch("notify_watcher.config.section", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def _route(self, state, bucket, arts):
        news.route(
            state, bucket=bucket, title="FilmX", articles=arts,
            scoring_cfg=SCORING, digest_cfg=DIGEST, cap=100,
            live_tag="clapper", live_title_prefix="Movie news",
        )

    def test_norm_headline_strips_publisher_suffix(self):
        self.assertEqual(
            news._norm_headline("The Film - Official Trailer - IGN Africa"),
            news._norm_headline("The Film - Official Trailer - IGN Benelux"))
        self.assertEqual(news._norm_headline("Plain headline"), "plain headline")
        self.assertEqual(news._norm_headline(""), "")

    def test_syndicated_copies_route_once(self):
        state, bucket = {}, {"FilmX": []}
        arts = [
            _art("a1", "FilmX release date set - IGN"),
            _art("a2", "FilmX release date set - IGN Africa"),
            _art("a3", "FilmX release date set - IGN Benelux"),
        ]
        with capture_pushes() as sent:
            self._route(state, bucket, arts)
        self.assertEqual(len(sent), 1)
        # All three ids are still recorded as seen so none re-fires next run.
        self.assertEqual(len(bucket["FilmX"]), 3)

    def test_live_cap_one_push_per_title_per_run(self):
        # Two DISTINCT high-tier stories in one run: first pushes live, the
        # second lands in the digest instead of a second live push.
        state, bucket = {}, {"FilmX": []}
        arts = [
            _art("s1", "FilmX release date set for June"),
            _art("s2", "sequel release date announced too"),
        ]
        with capture_pushes() as sent:
            self._route(state, bucket, arts)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(state.get(digest.BUFFER_KEY, [])), 1)

    def test_cap_does_not_suppress_across_runs(self):
        state, bucket = {}, {"FilmX": []}
        with capture_pushes() as sent:
            self._route(state, bucket, [_art("r1", "FilmX release date set")])
            self._route(state, bucket, [_art("r2", "new release date announced")])
        self.assertEqual(len(sent), 2)  # budget resets per run


if __name__ == "__main__":
    unittest.main()
