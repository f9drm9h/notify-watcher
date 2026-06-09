"""Tests for the Anthropic news watcher (notify_watcher.topics.anthropic_news)."""
from __future__ import annotations

import time
import unittest

from notify_watcher.topics import anthropic_news as an


class _Entry:
    def __init__(self, id, title, source_title, days_ago=None):
        self.id, self.title, self.link = id, title, f"http://x/{id}"
        self.source = {"title": source_title}
        if days_ago is not None:
            self.published_parsed = time.gmtime(time.time() - days_ago * 86400)


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


class FreshnessTest(unittest.TestCase):
    """Google News resurfaces years-old official posts under new URLs (a 2023
    'introducing Claude Pro' alerted in 2026); the age gate stops them."""

    def test_old_official_post_is_age_gated(self):
        entries = [
            _Entry("1", "Introducing Claude Pro", "Anthropic", days_ago=400),
            _Entry("2", "Introducing Claude Opus 4.9", "Anthropic", days_ago=2),
        ]
        got = [t for _, t, _ in an._official(entries, max_age_days=14)]
        self.assertEqual(got, ["Introducing Claude Opus 4.9"])

    def test_gate_disabled_keeps_everything(self):
        entries = [_Entry("1", "Introducing Claude Pro", "Anthropic", days_ago=400)]
        self.assertEqual(len(an._official(entries, max_age_days=0)), 1)

    def test_undated_entries_pass_the_gate(self):
        entries = [_Entry("1", "Post without a date", "Anthropic")]
        self.assertEqual(len(an._official(entries, max_age_days=14)), 1)


if __name__ == "__main__":
    unittest.main()
