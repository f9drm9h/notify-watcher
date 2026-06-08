"""Tests for the reminders engine (notify_watcher.topics.reminders)."""
from __future__ import annotations

import unittest
from datetime import date

from notify_watcher.topics import reminders as rem

TODAY = date(2026, 6, 8)


class NextOccurrenceTest(unittest.TestCase):
    def test_one_off_returns_base(self):
        self.assertEqual(rem._next_occurrence(date(2027, 1, 1), TODAY, ""), date(2027, 1, 1))

    def test_yearly_uses_this_year_if_upcoming(self):
        self.assertEqual(rem._next_occurrence(date(1990, 12, 25), TODAY, "yearly"), date(2026, 12, 25))

    def test_yearly_rolls_to_next_year_if_passed(self):
        self.assertEqual(rem._next_occurrence(date(1990, 1, 1), TODAY, "yearly"), date(2027, 1, 1))

    def test_yearly_feb29_falls_back_in_non_leap_year(self):
        # 2027 is not a leap year -> Feb 28.
        self.assertEqual(rem._next_occurrence(date(2000, 2, 29), date(2026, 3, 1), "yearly"), date(2027, 2, 28))


class DueTest(unittest.TestCase):
    def test_fires_exactly_on_a_lead_day(self):
        # 30 days before 2026-07-08 is 2026-06-08 (today).
        reminders = [{"name": "Passport", "date": "2026-07-08", "lead_days": [90, 30, 7]}]
        due = rem._due(reminders, TODAY)
        self.assertEqual(len(due), 1)
        key, name, occ, days_left, _ = due[0]
        self.assertEqual((name, days_left), ("Passport", 30))
        self.assertEqual(key, "Passport|2026-07-08|30")

    def test_no_fire_between_lead_days(self):
        reminders = [{"name": "X", "date": "2026-07-20", "lead_days": [90, 30, 7]}]  # 42 days out
        self.assertEqual(rem._due(reminders, TODAY), [])

    def test_day_of_fires(self):
        reminders = [{"name": "Renewal", "date": "2026-06-08", "lead_days": [7, 0]}]
        due = rem._due(reminders, TODAY)
        self.assertEqual(due[0][3], 0)

    def test_default_leads_applied(self):
        reminders = [{"name": "Y", "date": "2026-06-15"}]  # 7 days out, in default leads
        self.assertEqual(len(rem._due(reminders, TODAY)), 1)

    def test_past_one_off_is_silent(self):
        reminders = [{"name": "Old", "date": "2020-01-01"}]
        self.assertEqual(rem._due(reminders, TODAY), [])

    def test_malformed_entries_skipped(self):
        reminders = [{"name": "no date"}, {"date": "2026-06-15"}, {"name": "bad", "date": "nope"}]
        self.assertEqual(rem._due(reminders, TODAY), [])

    def test_yearly_birthday(self):
        reminders = [{"name": "Bday", "date": "1990-06-15", "recurring": "yearly", "lead_days": [7]}]
        due = rem._due(reminders, TODAY)
        self.assertEqual(due[0][2], date(2026, 6, 15))


if __name__ == "__main__":
    unittest.main()
