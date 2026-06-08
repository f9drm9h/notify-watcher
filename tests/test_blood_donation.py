"""Tests for the blood-donation timer (notify_watcher.topics.blood_donation)."""
from __future__ import annotations

import unittest
from datetime import date

from notify_watcher.topics import blood_donation as bd

INTERVAL = 56
RENOTIFY = 30


class ShouldNotifyTest(unittest.TestCase):
    def test_not_yet_eligible_is_silent(self):
        last = date(2026, 6, 1)
        notify, eligible = bd._should_notify(last, INTERVAL, RENOTIFY, None, date(2026, 6, 20))
        self.assertFalse(notify)
        self.assertEqual(eligible, date(2026, 7, 27))

    def test_eligible_and_never_notified_fires(self):
        last = date(2024, 12, 15)  # long past -> eligible
        notify, _ = bd._should_notify(last, INTERVAL, RENOTIFY, None, date(2026, 6, 8))
        self.assertTrue(notify)

    def test_recently_notified_is_suppressed(self):
        last = date(2024, 12, 15)
        notify, _ = bd._should_notify(last, INTERVAL, RENOTIFY, date(2026, 6, 1), date(2026, 6, 8))
        self.assertFalse(notify)  # only 7 days since last nudge

    def test_renotifies_after_window(self):
        last = date(2024, 12, 15)
        notify, _ = bd._should_notify(last, INTERVAL, RENOTIFY, date(2026, 5, 1), date(2026, 6, 8))
        self.assertTrue(notify)  # 38 days since last nudge

    def test_eligible_exactly_today(self):
        last = date(2026, 4, 13)  # +56 days = 2026-06-08
        notify, eligible = bd._should_notify(last, INTERVAL, RENOTIFY, None, date(2026, 6, 8))
        self.assertTrue(notify)
        self.assertEqual(eligible, date(2026, 6, 8))


if __name__ == "__main__":
    unittest.main()
