"""Tests for the token-subset relevance filter shared by games.py and movies.py.

_news_tokens / _news_relevant decide whether a Google News headline is actually
about a watched title. The two modules carry identical copies, so each case runs
against BOTH to guard against the copies drifting apart. Coverage includes the
stopword drop and the roman-numeral -> digit mapping ("Part II" matches "Part 2").
A regression here silently mis-routes news (wrong-title alerts, or missed ones).
"""
from __future__ import annotations

import unittest

from notify_watcher.topics import games, movies

# Both modules expose the same pair; subTest over both so a drift in either fails.
MODULES = (("games", games), ("movies", movies))


class NewsTokensTest(unittest.TestCase):
    def _tok(self, mod, text):
        return mod._news_tokens(text)

    def test_lowercases_and_splits_on_punctuation(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertEqual(self._tok(mod, "Spider-Man: Beyond"),
                                 {"spider", "man", "beyond"})

    def test_drops_stopwords(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                # "of" and "the" are stopwords; only meaningful tokens remain.
                self.assertEqual(self._tok(mod, "God of War"), {"god", "war"})

    def test_maps_roman_numerals_to_digits(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertEqual(self._tok(mod, "Grand Theft Auto VI"),
                                 {"grand", "theft", "auto", "6"})
                self.assertEqual(self._tok(mod, "Final Fantasy VII"),
                                 {"final", "fantasy", "7"})
                self.assertEqual(self._tok(mod, "The Batman Part II"),
                                 {"batman", "part", "2"})

    def test_all_stopwords_yields_empty(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertEqual(self._tok(mod, "the of a an"), set())

    def test_numbers_pass_through(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertEqual(self._tok(mod, "Dune 3"), {"dune", "3"})


class NewsRelevantTest(unittest.TestCase):
    def test_exact_subset_is_relevant(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertTrue(mod._news_relevant("God of War Ragnarok",
                                                   "God of War Ragnarok release date set"))

    def test_stopwords_do_not_block_match(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                # "of" is dropped from both sides, so it never needs to appear.
                self.assertTrue(mod._news_relevant("God of War", "God War sequel announced"))

    def test_roman_numeral_title_matches_arabic_headline(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertTrue(mod._news_relevant("Grand Theft Auto VI",
                                                   "Grand Theft Auto 6 trailer drops"))
                self.assertTrue(mod._news_relevant("The Batman Part II",
                                                   "The Batman Part 2 delayed to 2027"))

    def test_missing_distinctive_token_is_not_relevant(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                # "laufey" is absent from the headline -> not about this title.
                self.assertFalse(mod._news_relevant("God of War Laufey",
                                                    "God of War gets a new update"))

    def test_case_insensitive(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                self.assertTrue(mod._news_relevant("GOD OF WAR", "god of war ragnarok review"))

    def test_empty_title_is_not_relevant(self):
        for name, mod in MODULES:
            with self.subTest(module=name):
                # No meaningful tokens to require -> guard returns False, not True.
                self.assertFalse(mod._news_relevant("the of a", "anything at all"))


if __name__ == "__main__":
    unittest.main()
