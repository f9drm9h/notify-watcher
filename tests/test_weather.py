"""Tests for the tropical-weather watcher (notify_watcher.topics.weather)."""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import health
from notify_watcher.topics import weather

CFG = {
    "region_terms": ["dominican", "hispaniola", "caribbean"],
    "live_terms": ["warning", "watch"],
}


class _Entry:
    """Minimal stand-in for a feedparser entry (attribute access via getattr)."""
    def __init__(self, title="", summary="", link="", updated=""):
        self.title, self.summary, self.link, self.updated = title, summary, link, updated


class ClassifyTest(unittest.TestCase):
    def test_off_region_entries_are_ignored(self):
        entries = [
            _Entry("Tropical Storm Gulf forms off Texas", "warning for Texas coast"),
            _Entry("There are no tropical cyclones at this time."),
        ]
        self.assertEqual(weather._classify(entries, CFG), [])

    def test_region_with_warning_is_live(self):
        e = _Entry("Hurricane Maria Advisory",
                   "A Hurricane Warning is in effect for the Dominican Republic.")
        rows = weather._classify([e], CFG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "live")

    def test_region_without_warning_is_digest(self):
        e = _Entry("Atlantic Tropical Weather Outlook",
                   "A wave near the Caribbean has a low chance of development.")
        rows = weather._classify([e], CFG)
        self.assertEqual(rows[0][1], "digest")

    def test_dedup_key_changes_with_update_stamp(self):
        a = _Entry("Outlook", "caribbean", "http://x", updated="T1")
        b = _Entry("Outlook", "caribbean", "http://x", updated="T2")
        self.assertNotEqual(weather._dedup_key(a), weather._dedup_key(b))


RSS_ONE_ENTRY = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>NHC Atlantic</title>
<item><title>Atlantic Tropical Weather Outlook</title>
<guid>outlook-1</guid><link>https://nhc.noaa.gov/x</link>
<description>A wave near the Caribbean has a low chance of development.</description>
</item></channel></rss>"""


class HealthContractTest(unittest.TestCase):
    """run() reports its source outcome (notify_watcher.health)."""

    def _run(self, get):
        with mock.patch.object(weather.requests, "get", get), \
                mock.patch.object(weather.config, "section",
                                  side_effect=lambda n: CFG if n == "weather" else {}):
            return weather.run({})

    def test_fetch_failure_reports_source_failed(self):
        state = self._run(mock.Mock(side_effect=OSError("connection refused")))
        status = state[health.STATUS_KEY]["weather"]
        self.assertTrue(status["source_failed"])
        self.assertIn("NHC fetch failed", status["message"])

    def test_feed_reports_ok_with_prefilter_entry_count(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.content = RSS_ONE_ENTRY
        state = self._run(mock.Mock(return_value=resp))
        status = state[health.STATUS_KEY]["weather"]
        self.assertTrue(status["ok"])
        self.assertEqual(status["data_count"], 1)
        self.assertIn("last_data", state["topic_health"]["weather"])


if __name__ == "__main__":
    unittest.main()
