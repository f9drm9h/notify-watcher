"""Tests for the Golden Sun community tracker (notify_watcher.topics.golden_sun)."""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import unittest

from notify_watcher import news, scoring
from notify_watcher.topics import golden_sun as gs

TODAY = _dt.date(2026, 6, 11)

# Trimmed from the real Template:News wikitext on goldensunwiki.net: dated
# bullets with title templates, internal/external links, and the <noinclude>
# archive section that must never be read.
SAMPLE_WIKITEXT = """\
*'''6/8/2025:''' The [https://www.nintendo.com/us/whatever Nintendo Music app] for [[List of Consoles|Switch Online]] users uploads the [[Music|full soundtrack]] from {{GSTLATitle}}.
*'''1/16/2024:''' {{GSTitle}} and {{GSTLATitle}} are released for [[List of Consoles|Switch Online]]'s Expansion Pack, one day earlier than originally announced.
*not a dated bullet, must be skipped
[[:Template:News|'''More News...''']]
<noinclude>
----
*'''12/7/2018:''' ''[[Super Smash Bros. Ultimate]]'' is released worldwide.
</noinclude>
"""


class CleanWikitextTest(unittest.TestCase):
    def test_templates_links_and_quotes_rendered(self):
        raw = ("{{GSTitle}} and {{GSTLATitle}} on [[List of Consoles|Switch Online]] "
               "via [https://example.com the app] — '''big''' ''news'' [[Music]]")
        self.assertEqual(
            gs._clean_wikitext(raw),
            "Golden Sun and Golden Sun: The Lost Age on Switch Online "
            "via the app — big news Music",
        )

    def test_unknown_template_vanishes(self):
        self.assertEqual(gs._clean_wikitext("{{Mystery}} hello"), "hello")


class ParseWikiNewsTest(unittest.TestCase):
    def test_recent_bullet_kept_with_date_prefix(self):
        items = gs._parse_wiki_news(SAMPLE_WIKITEXT, 60, today=_dt.date(2025, 6, 20))
        self.assertEqual(len(items), 1)
        iid, headline = items[0]
        self.assertTrue(iid.startswith("gsu:"))
        self.assertEqual(
            headline,
            "6/8/2025: The Nintendo Music app for Switch Online users uploads "
            "the full soundtrack from Golden Sun: The Lost Age.",
        )

    def test_old_bullets_age_gated(self):
        # By TODAY (2026), every sample bullet is far past the 60-day window.
        self.assertEqual(gs._parse_wiki_news(SAMPLE_WIKITEXT, 60, today=TODAY), [])

    def test_noinclude_archive_never_read(self):
        # A huge window reaches the 2018 archive bullet by age — but it sits
        # after <noinclude>, so it must still be excluded.
        items = gs._parse_wiki_news(SAMPLE_WIKITEXT, 10000, today=TODAY)
        self.assertEqual(len(items), 2)
        self.assertNotIn("Smash", " ".join(h for _, h in items))

    def test_id_stable_for_same_text(self):
        a = gs._parse_wiki_news(SAMPLE_WIKITEXT, 10000, today=TODAY)
        b = gs._parse_wiki_news(SAMPLE_WIKITEXT, 10000, today=TODAY)
        self.assertEqual([i for i, _ in a], [i for i, _ in b])


class ParseRedditJsonTest(unittest.TestCase):
    @staticmethod
    def _payload(*posts):
        return {"data": {"children": [
            {"kind": "t3", "data": d} for d in posts
        ]}}

    def test_filters_by_min_score(self):
        payload = self._payload(
            {"name": "t3_a", "title": "Big ROM hack released", "score": 120,
             "permalink": "/r/GoldenSun/comments/a/"},
            {"name": "t3_b", "title": "low effort meme", "score": 12,
             "permalink": "/r/GoldenSun/comments/b/"},
            {"name": "t3_c", "title": "exactly at the bar", "score": 50,
             "permalink": "/r/GoldenSun/comments/c/"},
        )
        posts = gs._parse_reddit_json(payload, 50)
        self.assertEqual([p[0] for p in posts], ["t3_a"])  # >50, strictly
        self.assertEqual(posts[0][2], "https://www.reddit.com/r/GoldenSun/comments/a/")

    def test_malformed_children_skipped(self):
        payload = self._payload(
            {"title": "no name", "score": 99},
            {"name": "t3_x", "score": 99},               # no title
            {"name": "t3_y", "title": "no score"},
            {"name": "t3_ok", "title": "fine", "score": 80, "permalink": "/p/"},
        )
        self.assertEqual([p[0] for p in gs._parse_reddit_json(payload, 50)], ["t3_ok"])

    def test_not_a_listing_returns_empty(self):
        self.assertEqual(gs._parse_reddit_json({"error": 403}, 50), [])
        self.assertEqual(gs._parse_reddit_json({"data": {"children": "nope"}}, 50), [])


class RelevantTest(unittest.TestCase):
    def test_requires_game_name_in_headline(self):
        self.assertTrue(gs._relevant("Golden Sun remaster announced"))
        self.assertTrue(gs._relevant("GOLDEN SUN returns to Switch"))
        self.assertFalse(gs._relevant("Camelot announces new Mario Golf"))
        self.assertFalse(gs._relevant(""))


# Golden test for the live golden_sun_scoring config, mirroring
# test_games_scoring_config: editing monitors.json keywords (a no-code change)
# must not silently re-route the representative headlines below.
CONFIG = json.loads(
    (pathlib.Path(__file__).resolve().parent.parent / "monitors.json").read_text("utf-8")
)
GS_SCORING = CONFIG["golden_sun_scoring"]

CASES = [
    # The dream headlines: remaster/NSO language + official or press source.
    ("Golden Sun remaster officially announced for Switch", "Nintendo", "live"),
    ("Golden Sun remaster announced", "Nintendo Life", "live"),
    ("Golden Sun and The Lost Age confirmed for Switch Online Expansion Pack",
     "Golden Sun Universe", "live"),
    # Major ROM hack with a trailer pushes even from reddit.
    ("Golden Sun: The Lost Age PC port decompilation trailer", "Reddit r/GoldenSun", "live"),
    # Popular community posts (already cleared the upvote bar) -> digest.
    ("My Isaac cosplay from the convention", "Reddit r/GoldenSun", "digest"),
    ("What djinn setups did you go for in early-midgame?", "Reddit r/GoldenSun", "digest"),
    # Press chatter without a strong signal -> digest.
    ("Golden Sun retrospective: 25 years of Weyard", "IGN", "digest"),
    # Listicles, speculation, and unknown-blog filler -> dropped.
    ("Top 10 GBA RPGs ranked: Golden Sun", "", "drop"),
    ("Why Golden Sun deserves a comeback", "", "drop"),
    ("Golden Sun could return, fans want a remake", "", "drop"),
]


def _route_for(headline: str, publisher: str) -> str:
    key = news._source_weight_key(publisher, GS_SCORING.get("source_tiers", {}))
    _score, tier = scoring.score(headline, key, [], GS_SCORING)
    if tier in ("breakthrough", "high"):
        return "live"
    if tier == "moderate":
        return "digest"
    return "drop"


class GoldenSunScoringConfigTest(unittest.TestCase):
    def test_representative_headlines_route_as_expected(self):
        for headline, publisher, expected in CASES:
            with self.subTest(headline=headline):
                self.assertEqual(_route_for(headline, publisher), expected)

    def test_nintendo_life_is_press_not_official(self):
        tiers = GS_SCORING["source_tiers"]
        self.assertEqual(news._source_weight_key("Nintendo Life", tiers), "tier1")
        self.assertEqual(news._source_weight_key("Nintendo", tiers), "official")

    def test_no_penalty_term_is_substring_of_a_signal_term(self):
        signals = [t.lower()
                   for g in GS_SCORING.get("signal_bonuses", {}).values()
                   for t in g.get("terms", [])]
        penalties = [t.lower()
                     for g in GS_SCORING.get("noise_penalties", {}).values()
                     for t in g.get("terms", [])]
        for p in penalties:
            for s in signals:
                self.assertNotIn(p, s, f"penalty {p!r} is a substring of signal {s!r}")


if __name__ == "__main__":
    unittest.main()
