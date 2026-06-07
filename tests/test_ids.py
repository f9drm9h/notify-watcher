"""Tests for the compact seen-list id helper (notify_watcher.ids)."""
from __future__ import annotations

import unittest

from notify_watcher import ids


class IdsTest(unittest.TestCase):
    def test_short_is_deterministic_16_hex(self):
        h = ids.short("https://news.google.com/rss/articles/CBMiAbcdef...")
        self.assertEqual(len(h), ids.HASH_LEN)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))
        self.assertEqual(h, ids.short("https://news.google.com/rss/articles/CBMiAbcdef..."))

    def test_distinct_ids_distinct_hashes(self):
        self.assertNotEqual(ids.short("a"), ids.short("b"))

    def test_short_handles_empty(self):
        self.assertEqual(len(ids.short("")), ids.HASH_LEN)

    def test_normalize_hashes_raw_ids(self):
        raw = ["NDA12345:ORIG1", "https://example.com/a-very-long-article-id"]
        out = ids.normalize_seen(raw)
        self.assertEqual(out, [ids.short(raw[0]), ids.short(raw[1])])

    def test_normalize_is_idempotent_on_hashes(self):
        once = ids.normalize_seen(["NDA12345:ORIG1"])
        twice = ids.normalize_seen(once)
        self.assertEqual(once, twice)  # already-hashed entries are left alone

    def test_real_id_is_not_mistaken_for_a_hash(self):
        # Raw ids are long or contain non-hex, so they never look pre-hashed.
        self.assertFalse(ids._is_short("NDA12345:ORIG1"))  # contains ':' and 'N'
        self.assertFalse(ids._is_short("a" * 200))         # too long
        self.assertTrue(ids._is_short(ids.short("anything")))

    def test_substantial_size_reduction(self):
        # A realistic Google News id is ~200 chars; the hash is 16.
        raw_id = "https://news.google.com/rss/articles/" + "Q" * 180
        self.assertLess(len(ids.short(raw_id)) / len(raw_id), 0.1)


if __name__ == "__main__":
    unittest.main()
