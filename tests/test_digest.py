"""Tests for the daily digest buffer (notify_watcher.digest)."""
from __future__ import annotations

import datetime as _dt
import unittest

from notify_watcher import digest
from tests._util import capture_pushes

CFG = {"max_buffer": 5, "max_items_in_message": 3}


def _item(n: int, source: str = "Energy") -> dict:
    return {"title": f"headline {n}", "url": f"http://x/{n}", "source": source}


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


if __name__ == "__main__":
    unittest.main()
