"""Tests for the FDA collector's pure normalization (notify_watcher.topics.fda).

Focuses on the active-ingredient enrichment that makes monitors.json -> fda
keywords matchable: Drugs@FDA has no indication field, so the brand name alone
never contains a disease/agent keyword, but the active ingredient can.
"""
from __future__ import annotations

import unittest

from notify_watcher import scoring
from notify_watcher.topics import fda

# Hermetic scoring config mirroring the live monitors.json shape.
SCORING = {
    "source_weights": {"regulatory": 5},
    "signal_bonuses": {
        "action_terms": {"weight": 2, "terms": ["approves"]},
        "watch_match": {"weight": 2},
    },
    "noise_penalties": {},
    "thresholds": {"breakthrough": 8, "high": 6, "moderate": 4},
}
KEYWORDS = ["vaccine", "influenza", "flu", "cancer"]


def _result(app_no, brand, ingredients, stype="ORIG", snum="1"):
    return {
        "application_number": app_no,
        "products": [{"brand_name": brand,
                      "active_ingredients": [{"name": n} for n in ingredients]}],
        "submissions": [{"submission_status": "AP", "submission_type": stype,
                         "submission_number": snum, "submission_status_date": "20240101"}],
    }


class ActiveIngredientsTest(unittest.TestCase):
    def test_collects_unique_titlecased(self):
        res = {"products": [
            {"active_ingredients": [{"name": "PEMBROLIZUMAB"}]},
            {"active_ingredients": [{"name": "pembrolizumab"}]},  # dup, diff case
            {"active_ingredients": [{"name": "VITAMIN D"}]},
        ]}
        self.assertEqual(fda._active_ingredients(res), "Pembrolizumab, Vitamin D")

    def test_missing_is_empty_string(self):
        self.assertEqual(fda._active_ingredients({}), "")
        self.assertEqual(fda._active_ingredients({"products": [{}]}), "")


class ItemsTitleTest(unittest.TestCase):
    def test_title_includes_active_ingredient(self):
        items = fda._items({"results": [_result("BLA1", "Keytruda", ["PEMBROLIZUMAB"])]},
                           ("NDA", "BLA"))
        self.assertEqual(items[0]["title"], "FDA approves Keytruda (Pembrolizumab) (BLA1)")
        self.assertEqual(items[0]["source"], "Keytruda")
        self.assertEqual(items[0]["id"], "BLA1:ORIG1")

    def test_ingredient_omitted_when_same_as_brand(self):
        # A generic whose brand == ingredient shouldn't render "(X) (X)".
        items = fda._items({"results": [_result("NDA9", "Tofacitinib Citrate",
                                                ["TOFACITINIB CITRATE"])]}, ("NDA", "BLA"))
        self.assertEqual(items[0]["title"], "FDA approves Tofacitinib Citrate (NDA9)")

    def test_application_type_filter(self):
        payload = {"results": [
            _result("ANDA5", "Generic", ["X"]),  # excluded
            _result("NDA5", "Newdrug", ["Y"]),   # included
        ]}
        titles = [i["title"] for i in fda._items(payload, ("NDA", "BLA"))]
        self.assertEqual(len(titles), 1)
        self.assertIn("Newdrug", titles[0])


class KeywordMatchingTest(unittest.TestCase):
    def test_agent_keyword_matches_via_ingredient_and_boosts(self):
        # The whole point of the fix: an influenza-vaccine ingredient matches the
        # keyword list and adds the watch_match bonus on top of the live score.
        items = fda._items({"results": [_result("BLA3", "Flublok", ["INFLUENZA VACCINE"])]},
                           ("NDA", "BLA"))
        score, tier = scoring.score(items[0]["title"], "regulatory", KEYWORDS, SCORING)
        self.assertEqual(score, 5 + 2 + 2)   # regulatory + approves + watch_match
        self.assertEqual(tier, "breakthrough")

    def test_non_matching_approval_still_alerts_live(self):
        # No keyword matches the ingredient, but the approval still clears the
        # live tier (regulatory + "approves") — approvals are never silenced.
        items = fda._items({"results": [_result("BLA4", "Keytruda", ["PEMBROLIZUMAB"])]},
                           ("NDA", "BLA"))
        score, tier = scoring.score(items[0]["title"], "regulatory", KEYWORDS, SCORING)
        self.assertEqual(score, 5 + 2)       # regulatory + approves, no watch_match
        self.assertEqual(tier, "high")

    def test_supplement_is_moderate(self):
        items = fda._items({"results": [_result("NDA8", "Ozempic", ["SEMAGLUTIDE"],
                                                stype="SUPPL", snum="5")]}, ("NDA", "BLA"))
        score, tier = scoring.score(items[0]["title"], "regulatory", KEYWORDS, SCORING)
        self.assertEqual(tier, "moderate")   # no action term, no keyword


if __name__ == "__main__":
    unittest.main()
