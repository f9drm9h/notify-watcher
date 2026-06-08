"""Tests for the rocket-launch watcher (notify_watcher.topics.launches)."""
from __future__ import annotations

import datetime as dt
import unittest

from notify_watcher.topics import launches

NOW = dt.datetime(2026, 6, 8, 12, 0, tzinfo=dt.timezone.utc)
CFG = {"imminent_hours": 24, "skip_keywords": ["Starlink"]}


def _r(rid, name, net):
    return {"id": rid, "name": name, "net": net, "vidURLs": [{"url": f"http://watch/{rid}"}]}


class SelectTest(unittest.TestCase):
    def test_imminent_non_starlink_selected(self):
        rows = launches._select([_r("1", "Falcon 9 | Crew-12", "2026-06-08T20:00:00Z")], NOW, CFG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "1")

    def test_starlink_skipped(self):
        rows = launches._select([_r("2", "Falcon 9 | Starlink Group 10-35", "2026-06-08T20:00:00Z")], NOW, CFG)
        self.assertEqual(rows, [])

    def test_far_future_excluded(self):
        rows = launches._select([_r("3", "Ariane 6", "2026-06-20T20:00:00Z")], NOW, CFG)
        self.assertEqual(rows, [])

    def test_past_excluded(self):
        rows = launches._select([_r("4", "Vulcan", "2026-06-08T06:00:00Z")], NOW, CFG)
        self.assertEqual(rows, [])

    def test_bad_net_skipped(self):
        rows = launches._select([_r("5", "Mystery", "")], NOW, CFG)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
