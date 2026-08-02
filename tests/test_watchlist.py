"""Tests for the watchlist loader's state-overlay merge (notify_watcher.watchlist).

The file half is exercised implicitly by the games tests; these pin the
docs/design/05 addition: titles(category, state) merges the [Add to watchlist]
reply-button overlay (state["watchlist_extra"]) after the file's own titles,
and stays byte-identical without state.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import watchlist


def _patch_file(titles):
    return mock.patch.object(watchlist, "_load", return_value=list(titles))


class TitlesOverlayTest(unittest.TestCase):
    def test_overlay_titles_merge_after_file(self):
        state = {"watchlist_extra": {"games": [{"name": "Hollow Knight 2"}]}}
        with _patch_file(["Wolverine"]):
            self.assertEqual(watchlist.titles("games", state),
                             ["Wolverine", "Hollow Knight 2"])

    def test_without_state_is_file_only(self):
        with _patch_file(["Wolverine"]):
            self.assertEqual(watchlist.titles("games"), ["Wolverine"])

    def test_duplicate_overlay_title_is_dropped_case_insensitively(self):
        state = {"watchlist_extra": {"games": [{"name": "wolverine"}]}}
        with _patch_file(["Wolverine"]):
            self.assertEqual(watchlist.titles("games", state), ["Wolverine"])

    def test_malformed_overlay_entries_are_skipped(self):
        state = {"watchlist_extra": {"games": ["raw-string", {"x": 1}, None]}}
        with _patch_file(["Wolverine"]):
            self.assertEqual(watchlist.titles("games", state), ["Wolverine"])


if __name__ == "__main__":
    unittest.main()
