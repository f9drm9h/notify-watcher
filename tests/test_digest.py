"""Tests for the daily digest buffer (notify_watcher.digest)."""
from __future__ import annotations

import datetime as _dt
import unittest

from notify_watcher import digest
from tests._util import capture_pushes

CFG = {"max_buffer": 5, "max_items_in_message": 3}


def _item(n: int, source: str = "Energy", score: int = 0) -> dict:
    return {"title": f"headline {n}", "url": f"http://x/{n}", "source": source, "score": score}


class DigestTest(unittest.TestCase):
    def test_add_caps_buffer_keeping_newest(self):
        state: dict = {}
        for n in range(8):
            digest.add(state, _item(n), CFG)
        buf = state[digest.BUFFER_KEY]
        self.assertEqual(len(buf), 5)  # capped at max_buffer
        self.assertEqual(buf[0]["title"], "headline 3")  # oldest dropped
        self.assertEqual(buf[-1]["title"], "headline 7")

    def test_flush_empty_is_noop(self):
        state: dict = {}
        with capture_pushes() as sent:
            self.assertFalse(digest.flush(state, CFG))
        self.assertEqual(sent, [])
        self.assertNotIn(digest.LAST_SENT_KEY, state)  # empty flush doesn't stamp

    def test_flush_sends_groups_and_clears(self):
        state: dict = {}
        digest.add(state, _item(1, "Energy"), CFG)
        digest.add(state, _item(2, "FDA"), CFG)
        with capture_pushes() as sent:
            self.assertTrue(digest.flush(state, CFG))
        self.assertEqual(len(sent), 1)
        body = sent[0]["message"]
        self.assertIn("ENERGY", body)
        self.assertIn("FDA", body)
        self.assertEqual(state[digest.BUFFER_KEY], [])  # buffer cleared
        self.assertEqual(state[digest.LAST_SENT_KEY], _dt.date.today().isoformat())

    def test_flush_is_idempotent_per_day(self):
        state: dict = {}
        digest.add(state, _item(1), CFG)
        with capture_pushes() as sent:
            self.assertTrue(digest.flush(state, CFG))
            digest.add(state, _item(2), CFG)
            self.assertFalse(digest.flush(state, CFG))  # second flush same day
        self.assertEqual(len(sent), 1)

    def test_flush_reports_overflow_count(self):
        state: dict = {}
        for n in range(5):  # max_buffer=5, max_items_in_message=3
            digest.add(state, _item(n), CFG)
        with capture_pushes() as sent:
            digest.flush(state, CFG)
        self.assertIn("+2 more", sent[0]["message"])  # 5 buffered, 3 shown

    def test_add_stores_score(self):
        state: dict = {}
        digest.add(state, _item(1, score=7), CFG)
        self.assertEqual(state[digest.BUFFER_KEY][0]["score"], 7)
        # A caller that omits score defaults to 0, never raising.
        digest.add(state, {"title": "t", "url": "u", "source": "s"}, CFG)
        self.assertEqual(state[digest.BUFFER_KEY][1]["score"], 0)

    def test_flush_drops_lowest_score_on_overflow(self):
        # The bug fix: with more items than fit, the LEAST important are dropped
        # (previously buf[:N] kept the oldest and dropped the newest regardless
        # of importance). max_items_in_message=3.
        state: dict = {}
        digest.add(state, _item(1, "Energy", score=1), CFG)   # low
        digest.add(state, _item(2, "Energy", score=9), CFG)   # high
        digest.add(state, _item(3, "Energy", score=2), CFG)   # low
        digest.add(state, _item(4, "Energy", score=8), CFG)   # high
        with capture_pushes() as sent:
            digest.flush(state, CFG)
        body = sent[0]["message"]
        self.assertIn("headline 2", body)   # score 9 survives
        self.assertIn("headline 4", body)   # score 8 survives
        self.assertIn("headline 3", body)   # score 2 survives (3rd slot)
        self.assertNotIn("headline 1", body)  # score 1 dropped on overflow
        self.assertIn("+1 more", body)

    def test_add_per_source_cap_protects_low_volume_sources(self):
        # A chatty source must not evict a quiet one. With a per-source cap, a
        # flood from one source is trimmed to the cap (dropping its own lowest-
        # score items), leaving a different source's lone item untouched.
        cfg = {"max_buffer": 50, "max_per_source": 3, "max_items_in_message": 10}
        state: dict = {}
        digest.add(state, _item(99, "Energy", score=5), cfg)   # lone quiet item
        for n in range(10):                                     # chatty flood
            digest.add(state, _item(n, "Avengers", score=4), cfg)
        sources = [it["source"] for it in state[digest.BUFFER_KEY]]
        self.assertEqual(sources.count("Avengers"), 3)   # capped per source
        self.assertEqual(sources.count("Energy"), 1)     # quiet item survived

    def test_add_global_cap_drops_lowest_score(self):
        # Over the global ceiling, the least important item goes regardless of age.
        cfg = {"max_buffer": 3, "max_per_source": 99, "max_items_in_message": 10}
        state: dict = {}
        digest.add(state, _item(1, "A", score=1), cfg)   # lowest, oldest
        digest.add(state, _item(2, "B", score=5), cfg)
        digest.add(state, _item(3, "C", score=4), cfg)
        digest.add(state, _item(4, "D", score=3), cfg)   # pushes over cap of 3
        titles = [it["title"] for it in state[digest.BUFFER_KEY]]
        self.assertNotIn("headline 1", titles)  # score 1 evicted
        self.assertEqual(len(titles), 3)

    def test_flush_renders_detail_after_title(self):
        # Body-informative topics keep their detail when digested.
        state: dict = {}
        digest.add(state, {"title": "Reminder", "source": "Reminders",
                           "score": 5, "detail": "Mom's birthday (in 3 days)"}, CFG)
        with capture_pushes() as sent:
            digest.flush(state, CFG)
        self.assertIn("Reminder - Mom's birthday (in 3 days)", sent[0]["message"])

    def test_flush_without_detail_renders_title_only(self):
        # Collector/news items carry no detail and render exactly as before.
        state: dict = {}
        digest.add(state, _item(1, "Energy"), CFG)
        with capture_pushes() as sent:
            digest.flush(state, CFG)
        self.assertIn("  - headline 1", sent[0]["message"])

    def test_detail_is_truncated(self):
        state: dict = {}
        digest.add(state, {"title": "t", "source": "s", "detail": "x" * 500}, CFG)
        self.assertLessEqual(len(state[digest.BUFFER_KEY][0]["detail"]), digest._MAX_DETAIL)

    def test_flush_orders_by_score_highest_first(self):
        state: dict = {}
        digest.add(state, _item(1, "FDA", score=2), CFG)
        digest.add(state, _item(2, "Energy", score=9), CFG)
        with capture_pushes() as sent:
            digest.flush(state, CFG)
        body = sent[0]["message"]
        # The source with the top-scoring item leads the message.
        self.assertLess(body.index("ENERGY"), body.index("FDA"))


if __name__ == "__main__":
    unittest.main()
