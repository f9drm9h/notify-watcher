"""Tests for the Anthropic news watcher (notify_watcher.topics.anthropic_news)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import anthropic_news as an


class _Entry:
    def __init__(self, id, title, source_title):
        self.id, self.title, self.link = id, title, f"http://x/{id}"
        self.source = {"title": source_title}


class OfficialFilterTest(unittest.TestCase):
    def test_keeps_only_anthropic_source(self):
        entries = [
            _Entry("1", "Introducing Claude Opus 4.9", "Anthropic"),
            _Entry("2", "Anthropic might be powerful", "The Washington Post"),
            _Entry("3", "Claude Code update", "anthropic"),  # case-insensitive
        ]
        got = [t for _, t, _ in an._official(entries)]
        self.assertEqual(got, ["Introducing Claude Opus 4.9", "Claude Code update"])

    def test_entry_source_handles_missing(self):
        e = _Entry("9", "x", "Anthropic")
        e.source = None
        self.assertEqual(an._entry_source(e), "")


if __name__ == "__main__":
    unittest.main()
