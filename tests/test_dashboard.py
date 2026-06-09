"""Tests for the static dashboard renderer (notify_watcher.dashboard).

``summarize`` is the pure core: a synthetic state in, a view-model out, with the clock
injected so the 7-day window and relative ages are deterministic. These pin the action
counts, the day grouping (newest first, drops excluded), the priority-distribution
buckets, and the topic-health rows; plus a smoke test that ``render`` produces a
self-contained page embedding the events blob.
"""
from __future__ import annotations

import datetime as _dt
import json
import unittest

from notify_watcher import dashboard

NOW = _dt.datetime(2026, 6, 8, 14, 0, tzinfo=_dt.timezone.utc)


def _ev(ts, topic, title, action, score, detail="", url=""):
    return {"ts": ts, "topic": topic, "title": title, "source": topic.upper(),
            "severity": "high", "score": score, "action": action,
            "detail": detail, "url": url}


def _state():
    return {
        "event_log": [
            _ev("2026-06-08T13:00:00+00:00", "quakes", "Hurricane watch", "push", 95),
            _ev("2026-06-08T12:00:00+00:00", "ios", "iOS 26.1 released", "digest", 40),
            _ev("2026-06-08T11:00:00+00:00", "fx", "USD/DOP moved", "drop", 10),
            _ev("2026-06-07T09:00:00+00:00", "anthropic", "Claude Code", "push", 80,
                detail="big news", url="https://x"),
            # stale entry outside the 7-day window — excluded from counts/distribution
            _ev("2026-05-01T09:00:00+00:00", "old", "ancient", "push", 50),
        ],
        "digest_buffer": [
            {"title": "FDA approval", "source": "fda", "score": 70, "detail": ""},
            {"title": "GTA VI", "source": "games", "score": 15, "detail": "moved +115 days"},
        ],
        "digest_last_sent": "2026-06-07",
        "topic_health": {
            "fx": {"last_ok": "2026-06-08T13:00:00+00:00"},
            "movies": {"last_error": "fetch failed",
                       "last_error_ts": "2026-06-08T09:00:00+00:00"},
        },
        "last_run": {"ts": "2026-06-08T14:00:00+00:00", "ok": 27, "failed": 1},
    }


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.vm = dashboard.summarize(_state(), NOW)

    def test_action_counts_within_window(self):
        # the May 1 push is outside the 7d window -> not counted
        self.assertEqual(self.vm["counts"], {"push": 2, "digest": 1, "drop": 1})

    def test_distribution_buckets(self):
        d = self.vm["distribution"]
        self.assertEqual(d["90+"], 1)   # 95
        self.assertEqual(d["70+"], 1)   # 80
        self.assertEqual(d["40+"], 1)   # 40
        self.assertEqual(d["<40"], 1)   # 10 (the drop still counts toward distribution)

    def test_days_newest_first_and_drops_excluded(self):
        days = self.vm["days"]
        self.assertEqual(days[0]["label"], "June 8")
        self.assertEqual(days[1]["label"], "June 7")
        # the dropped fx item is not rendered as an alert row
        titles = [it["title"] for d in days for it in d["items"]]
        self.assertNotIn("USD/DOP moved", titles)
        self.assertIn("Hurricane watch", titles)

    def test_health_rows(self):
        rows = {h["topic"]: h for h in self.vm["health"]}
        self.assertTrue(rows["fx"]["ok"])
        self.assertEqual(rows["fx"]["age"], "1h ago")
        self.assertFalse(rows["movies"]["ok"])
        self.assertEqual(rows["movies"]["error"], "fetch failed")


class RenderTest(unittest.TestCase):
    def test_self_contained_page(self):
        html = dashboard.render(_state(), now=NOW)
        self.assertIn("<!doctype html>", html)
        self.assertIn("Hurricane watch", html)
        self.assertIn("June 8", html)
        # the score prefix and a clickable link are present
        self.assertIn(">95<", html)
        self.assertIn('href="https://x"', html)
        # events blob embedded for client-side search
        self.assertIn('id="events"', html)
        start = html.index('type="application/json">') + len('type="application/json">')
        end = html.index("</script>", start)
        json.loads(html[start:end])  # valid JSON

    def test_empty_state_renders(self):
        html = dashboard.render({}, now=NOW)
        self.assertIn("No alerts logged yet.", html)
        self.assertIn("Digest buffer is empty.", html)


if __name__ == "__main__":
    unittest.main()
