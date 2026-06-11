"""Tests for the monthly bill-reminder engine (notify_watcher.topics.bills)."""
from __future__ import annotations

import unittest
from datetime import date

from notify_watcher.topics import bills

TODAY = date(2026, 6, 20)


class NextDueTest(unittest.TestCase):
    def test_due_later_this_month(self):
        self.assertEqual(bills._next_due(25, TODAY), date(2026, 6, 25))

    def test_due_today_counts(self):
        self.assertEqual(bills._next_due(20, TODAY), date(2026, 6, 20))

    def test_passed_rolls_to_next_month(self):
        self.assertEqual(bills._next_due(10, TODAY), date(2026, 7, 10))

    def test_day_31_clamps_to_short_month(self):
        self.assertEqual(bills._next_due(31, date(2026, 6, 15)), date(2026, 6, 30))

    def test_day_30_clamps_in_february(self):
        # 2026 is not a leap year.
        self.assertEqual(bills._next_due(30, date(2026, 2, 10)), date(2026, 2, 28))

    def test_year_rollover(self):
        self.assertEqual(bills._next_due(5, date(2026, 12, 31)), date(2027, 1, 5))


class DueTest(unittest.TestCase):
    def test_fires_five_days_before(self):
        due = bills._due([{"id": "edeeste", "name": "EDEESTE", "due_day": 25}], TODAY)
        self.assertEqual(len(due), 1)
        key, name, occ, days_left, _ = due[0]
        self.assertEqual((name, days_left), ("EDEESTE", 5))
        self.assertEqual(key, "edeeste|2026-06-25|5")

    def test_fires_one_day_before(self):
        due = bills._due([{"name": "Agua", "due_day": 21}], TODAY)
        self.assertEqual(due[0][3], 1)
        # No id slug -> the name anchors the dedup key.
        self.assertEqual(due[0][0], "Agua|2026-06-21|1")

    def test_silent_between_lead_days(self):
        self.assertEqual(bills._due([{"name": "X", "due_day": 23}], TODAY), [])

    def test_silent_on_due_day_itself(self):
        # Default leads are [5, 1]; day-of is not one of them.
        self.assertEqual(bills._due([{"name": "X", "due_day": 20}], TODAY), [])

    def test_custom_lead_days(self):
        due = bills._due([{"name": "X", "due_day": 23, "lead_days": [3]}], TODAY)
        self.assertEqual(due[0][3], 3)

    def test_malformed_entries_skipped(self):
        bad = [
            {"due_day": 25},                      # no name
            {"name": "no day"},
            {"name": "zero", "due_day": 0},
            {"name": "oob", "due_day": 32},
            {"name": "text", "due_day": "soon"},
        ]
        self.assertEqual(bills._due(bad, TODAY), [])

    def test_note_carried_through(self):
        due = bills._due([{"name": "Net", "due_day": 25, "note": "autopay?"}], TODAY)
        self.assertEqual(due[0][4], "autopay?")


if __name__ == "__main__":
    unittest.main()
