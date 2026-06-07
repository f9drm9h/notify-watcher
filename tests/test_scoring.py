"""Tests for the deterministic importance scorer (notify_watcher.scoring)."""
from __future__ import annotations

import unittest

from notify_watcher import scoring

# Small synthetic config so these tests don't depend on monitors.json.
CFG = {
    "source_weights": {"strong": 5, "weak": 1},
    "signal_bonuses": {
        "action": {"weight": 2, "terms": ["approves", "recall"]},
        "watch_match": {"weight": 2},
        "safety": {"weight": 3, "terms": ["outbreak"]},
    },
    "noise_penalties": {
        "hype": {"weight": -2, "terms": ["could", "might"]},
    },
    "thresholds": {"breakthrough": 8, "high": 6, "moderate": 4},
}


class ScoringTest(unittest.TestCase):
    def test_source_weight_alone_sets_tier(self):
        total, tier = scoring.score("nothing notable", "strong", [], CFG)
        self.assertEqual(total, 5)
        self.assertEqual(tier, "moderate")  # 5 >= moderate(4), < high(6)

    def test_signal_bonus_promotes_tier(self):
        total, tier = scoring.score("agency approves drug", "strong", [], CFG)
        self.assertEqual(total, 7)  # 5 + action(2)
        self.assertEqual(tier, "high")

    def test_bonus_group_counts_once(self):
        # Two action terms in one headline still add the group weight only once.
        total, _ = scoring.score("approves and recall", "strong", [], CFG)
        self.assertEqual(total, 7)  # 5 + 2, not 5 + 4

    def test_watch_match_fires_on_keyword(self):
        total, _ = scoring.score("cancer breakthrough data", "weak", ["cancer"], CFG)
        self.assertEqual(total, 3)  # 1 + watch_match(2)
        total_no_kw, _ = scoring.score("cancer breakthrough data", "weak", [], CFG)
        self.assertEqual(total_no_kw, 1)  # no keywords -> no watch bonus

    def test_penalty_subtracts(self):
        total, tier = scoring.score("approves but might fail", "weak", [], CFG)
        self.assertEqual(total, 1)  # 1 + action(2) - hype(2)
        self.assertEqual(tier, "minor")

    def test_breakthrough_is_top_tier(self):
        # strong(5) + safety(3) = 8 -> breakthrough
        _, tier = scoring.score("outbreak reported", "strong", [], CFG)
        self.assertEqual(tier, "breakthrough")

    def test_substring_matching_is_intentional(self):
        # "recall" matches inside "recalls" by design (substring engine).
        total, _ = scoring.score("agency recalls product", "weak", [], CFG)
        self.assertEqual(total, 3)  # 1 + action(2)

    def test_empty_config_never_raises(self):
        self.assertEqual(scoring.score("anything", "missing", [], {}), (0, "minor"))

    def test_unknown_weight_key_scores_zero_base(self):
        total, tier = scoring.score("plain", "does-not-exist", [], CFG)
        self.assertEqual(total, 0)
        self.assertEqual(tier, "minor")


if __name__ == "__main__":
    unittest.main()
