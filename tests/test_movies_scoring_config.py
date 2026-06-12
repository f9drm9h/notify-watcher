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
# 2026-06 tuning: the four high-value events — a casting announcement, a
# release-date move (delay OR moved up), a cancellation, and a real trailer/
# teaser drop — carry weight 7 and push live ALONE from any source. A leak
# pushes live only when confirmation language meets a tier1/official source
# (otherwise it digests or drops). Rumor / box office / awards / review /
# listicle language is penalized into the digest or dropped.
CASES = [
    # High-signal events: live from any source.
    ("Marvel confirms Avengers: Doomsday release date for May 2026", "Marvel", "live"),
    ("New Superman trailer reveals the premiere date", "", "live"),
    ("Watch the new Superman teaser trailer", "", "live"),
    ("The Batman Part II has been delayed to 2027", "", "live"),
    ("Pedro Pascal joins the cast of Dune 3", "", "live"),
    ("Blade movie cancelled after years of delays at Marvel", "", "live"),
    ("Avatar 4 moved up to December 2030", "", "live"),
    ("Official trailer for The Odyssey released", "", "live"),
    # Leaks: live only when confirmed AND from a reliable outlet.
    ("Sony confirms Spider-Man trailer leak is real", "IGN", "live"),
    ("Sony confirms Spider-Man trailer leak is real", "", "digest"),
    ("Spider-Man: Brand New Day trailer 2 leaks online in full", "", "drop"),
    # Generic / low-value coverage: digest at best, mostly dropped.
    ("First look at Pedro Pascal in Fantastic Four", "", "drop"),
    ("First look at Pedro Pascal in Fantastic Four", "Variety", "digest"),
    ("Avatar 3 box office and a director interview", "Variety", "drop"),
    ("Dune 3 dominates the weekend box office", "", "drop"),
    ("Oscar nominations: Dune 3 leads the field", "", "drop"),
    ("New set photos from the Dune 3 shoot leak", "", "digest"),
    ("Superman early reactions are in", "", "drop"),
    # Rumor / speculation / listicle noise: dropped even with strong words.
    ("Casting rumor: Henry Cavill might join Avengers", "", "drop"),
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
