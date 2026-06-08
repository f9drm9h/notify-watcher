"""Tests for the ISS pass watcher (notify_watcher.topics.iss)."""
from __future__ import annotations

import datetime as dt
import unittest

from notify_watcher.topics import iss

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Santo_Domingo")  # UTC-4, no DST
except Exception:  # pragma: no cover
    TZ = dt.timezone(dt.timedelta(hours=-4))

NOW = dt.datetime(2026, 6, 8, 12, 0, tzinfo=dt.timezone.utc)
CFG = {"min_elevation_deg": 30}


def _p(start, max_el, aos=300, los=120):
    return {"start": start, "max_elevation": max_el, "aos_azimuth": aos, "los_azimuth": los}


class CompassTest(unittest.TestCase):
    def test_azimuth_to_compass(self):
        self.assertEqual(iss._compass(0), "N")
        self.assertEqual(iss._compass(90), "E")
        self.assertEqual(iss._compass(315), "NW")


class SelectTest(unittest.TestCase):
    def test_good_evening_pass_selected(self):
        # 2026-06-09T01:00Z = 2026-06-08 21:00 local (evening window).
        rows = iss._select([_p("2026-06-09T01:00:00Z", 45)], NOW, TZ, CFG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 45.0)

    def test_low_elevation_excluded(self):
        rows = iss._select([_p("2026-06-09T01:00:00Z", 12)], NOW, TZ, CFG)
        self.assertEqual(rows, [])

    def test_daytime_pass_excluded(self):
        # 2026-06-08T17:00Z = 13:00 local (midday, not a viewing window).
        rows = iss._select([_p("2026-06-08T17:00:00Z", 60)], NOW, TZ, CFG)
        self.assertEqual(rows, [])

    def test_beyond_24h_excluded(self):
        rows = iss._select([_p("2026-06-10T01:00:00Z", 60)], NOW, TZ, CFG)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
