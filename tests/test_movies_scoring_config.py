"""Golden test for the live movies_scoring config in monitors.json.

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
MOVIES = CONFIG["movies_scoring"]

# (headline, publisher, expected_route) where route is live / digest / drop.
CASES = [
    ("Marvel confirms Avengers: Doomsday release date for May 2026", "Marvel", "live"),
    ("Watch the new Superman teaser trailer", "", "live"),
    ("The Batman Part II has been delayed to 2027", "", "live"),
    ("First look at Pedro Pascal in Fantastic Four", "", "live"),
    ("Pedro Pascal joins the cast of Dune 3", "", "digest"),
    ("Avatar 3 box office and a director interview", "Variety", "digest"),
    ("New set photos from the Dune 3 shoot leak", "", "digest"),
    ("Superman early reactions are in", "", "digest"),
    ("Here is why Blade could be delayed again", "", "drop"),
    ("Top 10 most anticipated movies of 2026 ranked", "", "drop"),
    ("10 reasons why The Batman 2 deserves a sequel", "", "drop"),
    ("Avengers: everything we know so far", "", "drop"),
]


def _route_for(headline: str, publisher: str) -> str:
    key = news._source_weight_key(publisher, MOVIES.get("source_tiers", {}))
    _score, tier = scoring.score(headline, key, [], MOVIES)
    if tier in ("breakthrough", "high"):
        return "live"
    if tier == "moderate":
        return "digest"
    return "drop"


class MoviesScoringConfigTest(unittest.TestCase):
    def test_representative_headlines_route_as_expected(self):
        for headline, publisher, expected in CASES:
            with self.subTest(headline=headline):
                self.assertEqual(_route_for(headline, publisher), expected)

    def test_no_penalty_term_is_substring_of_a_signal_term(self):
        # Guards the substring-collision footgun: a penalty term must not appear
        # inside a positive signal term (the "review" in "preview" class of bug).
        signals = [t.lower()
                   for g in MOVIES.get("signal_bonuses", {}).values()
                   for t in g.get("terms", [])]
        penalties = [t.lower()
                     for g in MOVIES.get("noise_penalties", {}).values()
                     for t in g.get("terms", [])]
        for p in penalties:
            for s in signals:
                self.assertNotIn(p, s, f"penalty {p!r} is a substring of signal {s!r}")


if __name__ == "__main__":
    unittest.main()
