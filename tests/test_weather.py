"""Tests for the tropical-weather watcher (notify_watcher.topics.weather)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import weather

CFG = {
    "region_terms": ["dominican", "hispaniola", "caribbean"],
    "live_terms": ["warning", "watch"],
}


class _Entry:
    """Minimal stand-in for a feedparser entry (attribute access via getattr)."""
    def __init__(self, title="", summary="", link="", updated=""):
        self.title, self.summary, self.link, self.updated = title, summary, link, updated


class ClassifyTest(unittest.TestCase):
    def test_off_region_entries_are_ignored(self):
        entries = [
            _Entry("Tropical Storm Gulf forms off Texas", "warning for Texas coast"),
            _Entry("There are no tropical cyclones at this time."),
        ]
        self.assertEqual(weather._classify(entries, CFG), [])

    def test_region_with_warning_is_live(self):
        e = _Entry("Hurricane Maria Advisory",
                   "A Hurricane Warning is in effect for the Dominican Republic.")
        rows = weather._classify([e], CFG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "live")

    def test_region_without_warning_is_digest(self):
        e = _Entry("Atlantic Tropical Weather Outlook",
                   "A wave near the Caribbean has a low chance of development.")
        rows = weather._classify([e], CFG)
        self.assertEqual(rows[0][1], "digest")

    def test_dedup_key_changes_with_update_stamp(self):
        a = _Entry("Outlook", "caribbean", "http://x", updated="T1")
        b = _Entry("Outlook", "caribbean", "http://x", updated="T2")
        self.assertNotEqual(weather._dedup_key(a), weather._dedup_key(b))


if __name__ == "__main__":
    unittest.main()
