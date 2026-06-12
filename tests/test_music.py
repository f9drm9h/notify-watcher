"""Tests for the music watcher (notify_watcher.topics.music)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import config, control
from notify_watcher.topics import music


class PickSeedTest(unittest.TestCase):
    def test_rotates_by_day_of_year(self):
        artists = ["A", "B", "C"]
        self.assertEqual(music._pick_seed(artists, 0), "A")
        self.assertEqual(music._pick_seed(artists, 1), "B")
        self.assertEqual(music._pick_seed(artists, 3), "A")  # wraps

    def test_empty_seed_is_none(self):
        self.assertIsNone(music._pick_seed([], 5))


class PickRecommendationTest(unittest.TestCase):
    def setUp(self):
        self.related = [
            {"id": 1, "name": "Already In Library"},
            {"id": 2, "name": "Already Recommended"},
            {"id": 3, "name": "Fresh Artist"},
        ]
        self.seed_set = {"already in library"}
        self.seen_ids = {2}

    def test_picks_first_truly_new_artist(self):
        rec = music._pick_recommendation(self.related, self.seed_set, self.seen_ids)
        self.assertEqual(rec["id"], 3)

    def test_returns_none_when_all_known(self):
        rec = music._pick_recommendation(
            self.related[:2], self.seed_set, self.seen_ids)
        self.assertIsNone(rec)

    def test_skips_blank_names(self):
        rec = music._pick_recommendation(
            [{"id": 9, "name": ""}, {"id": 3, "name": "Fresh Artist"}],
            self.seed_set, self.seen_ids)
        self.assertEqual(rec["id"], 3)

    def test_rerolls_past_an_ignored_artist(self):
        # [Not my thing] on a past pick: that artist never comes back; the
        # next candidate is chosen instead.
        state: dict = {}
        oid = control.register_offer(state, "artist", "Fresh Artist",
                                     {"name": "Fresh Artist"})
        control.cmd_ignore(oid, state)
        rec = music._pick_recommendation(
            self.related + [{"id": 4, "name": "Backup Artist"}],
            self.seed_set, self.seen_ids, state["ignored"])
        self.assertEqual(rec["name"], "Backup Artist")


class ArtistsMergeTest(unittest.TestCase):
    def test_followed_overlay_merges_after_config(self):
        state: dict = {}
        oid = control.register_offer(state, "artist", "Mitski",
                                     {"name": "Mitski"})
        control.cmd_add(oid, state)
        with mock.patch.object(config, "section",
                               return_value={"followed_artists": ["Bo Burnham"]}):
            self.assertEqual(music._artists(state), ["Bo Burnham", "Mitski"])

    def test_overlay_duplicate_of_config_is_dropped(self):
        state = {"follows": {"artists": [{"name": "bo burnham"}]}}
        with mock.patch.object(config, "section",
                               return_value={"followed_artists": ["Bo Burnham"]}):
            self.assertEqual(music._artists(state), ["Bo Burnham"])


if __name__ == "__main__":
    unittest.main()
