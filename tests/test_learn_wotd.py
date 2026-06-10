"""Unit tests for the word-of-the-day parsing helpers in learn.py.

All tests are pure (no network calls) and use only stdlib unittest.
"""
from __future__ import annotations

import unittest

from notify_watcher.topics import learn

# ---------------------------------------------------------------------------
# Minimal Atom feed fixtures
# ---------------------------------------------------------------------------

_ATOM_NS = "http://www.w3.org/2005/Atom"

FEED_WITH_DATED_TITLE = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{_ATOM_NS}">
  <title>Wiktionary: Word of the day</title>
  <entry>
    <title>June 10, 2026: ephemeral</title>
    <content type="html">&lt;p&gt;&lt;b&gt;ephemeral&lt;/b&gt; /ɪˈfɛm.ər.əl/ &lt;i&gt;adjective&lt;/i&gt;&lt;/p&gt;\
&lt;p&gt;Lasting for only a short time; transitory.&lt;/p&gt;</content>
  </entry>
</feed>"""

FEED_WITH_PLAIN_TITLE = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{_ATOM_NS}">
  <entry>
    <title>sanguine</title>
    <summary type="html">&lt;p&gt;Optimistic or positive, especially in a difficult situation.&lt;/p&gt;</summary>
  </entry>
</feed>"""

FEED_WITH_NO_NAMESPACE = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed>
  <entry>
    <title>June 10, 2026: laconic</title>
    <content>Using very few words; brief and to the point.</content>
  </entry>
</feed>"""

FEED_WITH_EM_DASH_TITLE = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{_ATOM_NS}">
  <entry>
    <title>June 10, 2026 — cogent</title>
    <content type="html">Clear, logical, and convincing.</content>
  </entry>
</feed>"""

FEED_EMPTY = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{_ATOM_NS}">
</feed>"""

FEED_MALFORMED = "this is not XML at all <<<"


class ParseWotdXmlTest(unittest.TestCase):
    """Tests for _parse_wotd_xml() — the pure Atom parsing helper."""

    def test_extracts_word_from_dated_title(self):
        result = learn._parse_wotd_xml(FEED_WITH_DATED_TITLE)
        self.assertIsNotNone(result)
        self.assertEqual(result["word"], "ephemeral")

    def test_extracts_body_strips_html_tags(self):
        result = learn._parse_wotd_xml(FEED_WITH_DATED_TITLE)
        self.assertIsNotNone(result)
        self.assertIn("ephemeral", result["body"])
        self.assertIn("Lasting for only a short time", result["body"])
        self.assertNotIn("<p>", result["body"])
        self.assertNotIn("<b>", result["body"])

    def test_plain_title_kept_as_word(self):
        result = learn._parse_wotd_xml(FEED_WITH_PLAIN_TITLE)
        self.assertIsNotNone(result)
        self.assertEqual(result["word"], "sanguine")

    def test_summary_used_when_no_content(self):
        result = learn._parse_wotd_xml(FEED_WITH_PLAIN_TITLE)
        self.assertIsNotNone(result)
        self.assertIn("Optimistic", result["body"])

    def test_feed_without_atom_namespace(self):
        result = learn._parse_wotd_xml(FEED_WITH_NO_NAMESPACE)
        self.assertIsNotNone(result)
        self.assertEqual(result["word"], "laconic")
        self.assertIn("Using very few words", result["body"])

    def test_em_dash_as_separator(self):
        result = learn._parse_wotd_xml(FEED_WITH_EM_DASH_TITLE)
        self.assertIsNotNone(result)
        self.assertEqual(result["word"], "cogent")

    def test_returns_none_when_no_entry(self):
        self.assertIsNone(learn._parse_wotd_xml(FEED_EMPTY))

    def test_returns_none_on_malformed_xml(self):
        self.assertIsNone(learn._parse_wotd_xml(FEED_MALFORMED))


class FormatVocabEntryTest(unittest.TestCase):
    """Tests for _format_vocab_entry() — the local-fallback formatter."""

    _FULL_ENTRY = {
        "word": "mellifluous",
        "pronunciation": "/məˈlɪf.lu.əs/",
        "pos": "adjective",
        "definition": "Sweet or musical; pleasantly smooth in sound.",
        "example": "Her mellifluous voice made even announcements sound like poetry.",
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


class StripTagsTest(unittest.TestCase):
    """Tests for _strip_tags() — the HTML-stripping utility."""

    def test_removes_tags(self):
        self.assertEqual(learn._strip_tags("<b>word</b>"), "word")

    def test_collapses_whitespace(self):
        self.assertEqual(learn._strip_tags("<p>one</p>  <p>two</p>"), "one two")

    def test_empty_string(self):
        self.assertEqual(learn._strip_tags(""), "")

    def test_no_tags_passes_through(self):
        self.assertEqual(learn._strip_tags("plain text"), "plain text")

    def test_nested_tags(self):
        self.assertEqual(learn._strip_tags("<div><p><b>deep</b></p></div>"), "deep")


if __name__ == "__main__":
    unittest.main()
