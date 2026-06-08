"""Tests for the DR holidays watcher (notify_watcher.topics.holidays)."""
from __future__ import annotations

import unittest
from datetime import date

from notify_watcher.topics import holidays

TODAY = date(2026, 6, 8)
H = [
    {"date": "2026-06-09", "localName": "Corpus Christi", "name": "Corpus Christi"},
    {"date": "2026-06-08", "localName": "Test Today"},
    {"date": "2026-06-20", "localName": "Far Off"},
    {"date": "bad-date", "localName": "Malformed"},
]


class DueTest(unittest.TestCase):
    def test_fires_on_lead_days_only(self):
        due = holidays._due(H, TODAY, [1, 0])
        names = sorted(n for _, n, _, _ in due)
        self.assertEqual(names, ["Corpus Christi", "Test Today"])

    def test_day_counts_and_keys(self):
        due = {k: d for k, _, _, d in holidays._due(H, TODAY, [1, 0])}
        self.assertIn("2026-06-09|1", due)
        self.assertEqual(due["2026-06-09|1"], 1)
        self.assertEqual(due["2026-06-08|0"], 0)

    def test_no_match_outside_leads(self):
        self.assertEqual(holidays._due(H, TODAY, [7]), [])

    def test_localname_fallback_to_name(self):
        h = [{"date": "2026-06-09", "name": "OnlyName"}]
        self.assertEqual(holidays._due(h, TODAY, [1])[0][1], "OnlyName")


if __name__ == "__main__":
    unittest.main()
