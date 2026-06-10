"""Unit tests for the word-of-the-day channel in learn.py.

All tests are pure (no network calls) and use only stdlib unittest.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import learn

# day_of_year(Jan 6) == 6 and 6 % len(CHANNELS) == 6, the Word of the Day slot.
_WOTD_DAY = _dt.date(2026, 1, 6)


class FormatVocabEntryTest(unittest.TestCase):
    """Tests for _format_vocab_entry() — the structured entry formatter."""

    _FULL_ENTRY = {
        "word": "mellifluous",
        "pronunciation": "/məˈlɪf.lu.əs/",
        "pos": "adjective",
        "definition": "Sweet or musical; pleasantly smooth in sound.",
        "example": "Her mellifluous voice made even announcements sound like poetry.",
        "src": "Merriam-Webster",
    }

    def test_word_appears_on_first_line(self):
        result = learn._format_vocab_entry(self._FULL_ENTRY)
        lines = result.splitlines()
        self.assertEqual(lines[0], "mellifluous")

    def test_pronunciation_and_pos_on_second_line(self):
        result = learn._format_vocab_entry(self._FULL_ENTRY)
        lines = result.splitlines()
        self.assertIn("/m", lines[1])
        self.assertIn("adjective", lines[1])
        self.assertIn(" · ", lines[1])

    def test_definition_present(self):
        result = learn._format_vocab_entry(self._FULL_ENTRY)
        self.assertIn("Sweet or musical", result)

    def test_example_quoted(self):
        result = learn._format_vocab_entry(self._FULL_ENTRY)
        self.assertIn('"Her mellifluous voice', result)

    def test_src_attributed_on_last_line(self):
        result = learn._format_vocab_entry(self._FULL_ENTRY)
        self.assertEqual(result.splitlines()[-1], "(Source: Merriam-Webster)")

    def test_missing_src_omits_attribution(self):
        entry = {k: v for k, v in self._FULL_ENTRY.items() if k != "src"}
        result = learn._format_vocab_entry(entry)
        self.assertNotIn("Source", result)

    def test_missing_pronunciation_skips_meta_line(self):
        entry = {**self._FULL_ENTRY, "pronunciation": "", "pos": ""}
        result = learn._format_vocab_entry(entry)
        lines = result.splitlines()
        # Second line should be the definition, not a blank meta line
        self.assertIn("Sweet or musical", lines[1])

    def test_only_pos_no_pronunciation(self):
        entry = {**self._FULL_ENTRY, "pronunciation": ""}
        result = learn._format_vocab_entry(entry)
        lines = result.splitlines()
        self.assertEqual(lines[1], "adjective")

    def test_falls_back_to_text_field_when_no_definition(self):
        entry = {"word": "ennui", "text": "listlessness from lack of excitement"}
        result = learn._format_vocab_entry(entry)
        self.assertIn("listlessness", result)

    def test_empty_entry_returns_empty_string(self):
        result = learn._format_vocab_entry({})
        self.assertEqual(result, "")


class WotdFactTest(unittest.TestCase):
    """Tests for _wotd_fact() against the real data/vocabulary.json."""

    def test_returns_label_and_body(self):
        label, body = learn._wotd_fact(_WOTD_DAY)
        self.assertEqual(label, "Word of the Day")
        self.assertTrue(body)
        self.assertGreaterEqual(len(body.splitlines()), 2)  # word + at least definition

    def test_deterministic_per_day(self):
        self.assertEqual(learn._wotd_fact(_WOTD_DAY), learn._wotd_fact(_WOTD_DAY))

    def test_rotates_across_days(self):
        bodies = {learn._wotd_fact(_WOTD_DAY + _dt.timedelta(days=n))[1]
                  for n in range(3)}
        self.assertEqual(len(bodies), 3)

    def test_empty_kb_returns_empty(self):
        with mock.patch.object(learn.kb, "load", return_value=[]):
            self.assertEqual(learn._wotd_fact(_WOTD_DAY), ("", ""))


class CuratedFactDispatchTest(unittest.TestCase):
    """The Word of the Day slot must bypass the generic reword path."""

    def test_wotd_day_selects_wotd_channel(self):
        label, _ = learn._curated_fact(_WOTD_DAY)
        self.assertEqual(label, "Word of the Day")

    def test_wotd_is_never_llm_reworded(self):
        with mock.patch.object(learn.summarize, "one_line") as reword:
            learn._curated_fact(_WOTD_DAY)
        reword.assert_not_called()

    def test_other_days_use_other_channels(self):
        with mock.patch.object(learn.summarize, "one_line", return_value=None):
            label, fact = learn._curated_fact(_WOTD_DAY + _dt.timedelta(days=1))
        self.assertNotEqual(label, "Word of the Day")
        self.assertTrue(fact)


if __name__ == "__main__":
    unittest.main()
