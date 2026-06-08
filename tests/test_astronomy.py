"""Tests for the astronomy almanac (notify_watcher.topics.astronomy)."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from notify_watcher.topics import astronomy as astro


class MoonTest(unittest.TestCase):
    def test_phase_fraction_in_range(self):
        f = astro._phase_fraction(date(2026, 6, 8))
        self.assertTrue(0.0 <= f < 1.0)

    def test_exactly_one_full_moon_per_cycle(self):
        # Walk a full synodic month; expect exactly one 'full' and one 'new'.
        start = date(2026, 1, 1)
        events = [astro._moon_event(start + timedelta(days=i)) for i in range(30)]
        self.assertEqual(events.count("full"), 1)
        self.assertEqual(events.count("new"), 1)


class EventsTest(unittest.TestCase):
    def test_recurring_meteor_peak(self):
        msgs = astro._events_today(date(2026, 8, 12))
        self.assertTrue(any("Perseids" in m for m in msgs))

    def test_one_off_eclipse(self):
        msgs = astro._events_today(date(2026, 3, 3))
        self.assertTrue(any("lunar eclipse" in m for m in msgs))

    def test_ordinary_day_quiet(self):
        # A day with no table event and no moon crossing yields nothing.
        d = date(2026, 6, 25)
        if astro._moon_event(d) is None:
            self.assertEqual(astro._events_today(d), [])


if __name__ == "__main__":
    unittest.main()
