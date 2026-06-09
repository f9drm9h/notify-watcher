"""Tests for the energy collector's feed-entry mapping (notify_watcher.topics.energy).

_entries_to_items turns feedparser entries into the monitor-item dicts the shared
collector engine consumes. Captured-shape entries (a feedparser entry is just an
attribute bag) pin the id/link fallback and source/weight tagging so a silent
upstream feed change is caught in CI rather than surfacing as a missed alert.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from notify_watcher.topics import energy


def _entry(**kw) -> SimpleNamespace:
    """A stand-in for a feedparser entry (attribute access, missing attrs absent)."""
    return SimpleNamespace(**kw)


class EntriesToItemsTest(unittest.TestCase):
    def test_maps_fields_and_tags_source_weight(self):
        entries = [_entry(id="g1", title="Grid upgrade", link="http://x/1")]
        items = energy._entries_to_items(entries, "EIA Today in Energy", "gov_data")
        self.assertEqual(items, [{
            "id": "g1",
            "title": "Grid upgrade",
            "url": "http://x/1",
            "source": "EIA Today in Energy",
            "weight": "gov_data",
        }])

    def test_id_falls_back_to_link_when_guid_absent(self):
        entries = [_entry(title="No GUID", link="http://x/2")]  # no `id`
        items = energy._entries_to_items(entries, "Src", "trade")
        self.assertEqual(items[0]["id"], "http://x/2")
        self.assertEqual(items[0]["url"], "http://x/2")

    def test_entry_with_no_id_and_no_link_is_dropped(self):
        entries = [_entry(title="orphan")]  # neither id nor link
        self.assertEqual(energy._entries_to_items(entries, "Src", "trade"), [])

    def test_empty_id_string_falls_through_to_link(self):
        entries = [_entry(id="", link="http://x/3", title="t")]
        self.assertEqual(energy._entries_to_items(entries, "Src", "trade")[0]["id"], "http://x/3")

    def test_missing_title_becomes_empty_string(self):
        entries = [_entry(id="g4", link="http://x/4")]  # no title
        self.assertEqual(energy._entries_to_items(entries, "Src", "trade")[0]["title"], "")

    def test_multiple_entries_preserve_order(self):
        entries = [_entry(id="a", title="A", link="u/a"),
                   _entry(id="b", title="B", link="u/b")]
        ids = [it["id"] for it in energy._entries_to_items(entries, "Src", "trade")]
        self.assertEqual(ids, ["a", "b"])

    def test_empty_entry_list(self):
        self.assertEqual(energy._entries_to_items([], "Src", "trade"), [])


if __name__ == "__main__":
    unittest.main()
