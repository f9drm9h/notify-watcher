"""Golden test for the live games_scoring config in monitors.json.

Unlike the hermetic scoring tests, this loads the REAL config and pins how a
set of representative headlines route. It exists so that editing the keyword /
weight lists in monitors.json (a frequent, no-code change) can't silently break
tiering — e.g. re-introducing a substring collision like "review" inside
"preview". If you intentionally change routing, update the expectations here.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from notify_watcher import news, scoring

CONFIG = json.loads(
    (pathlib.Path(__file__).resolve().parent.parent / "monitors.json").read_text("utf-8")
)
GAMES = CONFIG["games_scoring"]

# (headline, publisher, expected_route) where route is live / digest / drop.
CASES = [
    ("Rockstar confirms GTA 6 release date for fall 2026", "Rockstar", "live"),
    ("Watch the new GTA 6 gameplay trailer", "", "live"),
    ("GTA 6 has been delayed to 2026", "", "live"),
    ("GTA 6 gameplay leaked online", "", "live"),
    ("Marvel's Wolverine new screenshots revealed", "", "live"),
    ("PlayStation Blog: God of War major update announced", "PlayStation Blog", "live"),
    ("GTA 6 Steam page goes live with wishlist", "", "digest"),
    ("GTA 6 developer interview on the map", "IGN", "digest"),
    ("GTA 6 preview: our hands-on impressions", "", "digest"),
    ("Here is why GTA 6 could be delayed again", "", "drop"),
    ("Top 10 most anticipated games ranked: GTA 6", "", "drop"),
    ("10 GTA 6 features we want to see", "", "drop"),
    ("GTA 6: everything we know so far", "", "drop"),
]


def _route_for(headline: str, publisher: str) -> str:
    key = news._source_weight_key(publisher, GAMES.get("source_tiers", {}))
    _score, tier = scoring.score(headline, key, [], GAMES)
    if tier in ("breakthrough", "high"):
        return "live"
    if tier == "moderate":
        return "digest"
    return "drop"


class GamesScoringConfigTest(unittest.TestCase):
    def test_representative_headlines_route_as_expected(self):
        for headline, publisher, expected in CASES:
            with self.subTest(headline=headline):
                self.assertEqual(_route_for(headline, publisher), expected)

    def test_no_penalty_term_is_substring_of_a_signal_term(self):
        # Guards the substring-collision footgun: a penalty term must not appear
        # inside a positive signal term (the "review" in "preview" class of bug).
        signals = [t.lower()
                   for g in GAMES.get("signal_bonuses", {}).values()
                   for t in g.get("terms", [])]
        penalties = [t.lower()
                     for g in GAMES.get("noise_penalties", {}).values()
                     for t in g.get("terms", [])]
        for p in penalties:
            for s in signals:
                self.assertNotIn(p, s, f"penalty {p!r} is a substring of signal {s!r}")


if __name__ == "__main__":
    unittest.main()
