"""Tests for the health-tip provider (notify_watcher.topics.health_tip).

health_tip is spark's "health" content leg: topics/spark.py owns the 6-hour
firing gate and calls send_tip when its rotation lands on a health tip. The
tip text comes from the vetted KB (data/health_tips.json, read for real — no
network); the optional AI rewording is patched out so the verbatim-fallback
path is what's under test. The per-day guard stays: at most one tip per
calendar day even if two rotation slots land on the same day.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher.topics import health_tip
from tests._util import capture_pushes


class SendTipTest(unittest.TestCase):
    def setUp(self):
        # No provider key in tests: send the vetted tip verbatim.
        self._ai = mock.patch.object(health_tip.summarize, "one_line",
                                     return_value=None)
        self._ai.start()
        self.addCleanup(self._ai.stop)

    def test_sends_one_tip_under_spark_topic_and_stamps_the_day(self):
        state: dict = {}
        with capture_pushes() as sent:
            self.assertTrue(health_tip.send_tip(state))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Health tip")
        self.assertEqual(sent[0]["topic"], "spark")
        self.assertTrue(sent[0]["message"])  # a real tip from the KB
        self.assertEqual(state[health_tip.STATE_KEY], health_tip._today())

    def test_same_day_resend_is_refused(self):
        # Two spark health slots can land on one calendar day (~18h apart);
        # the second must return False so spark retries on a later window.
        state = {health_tip.STATE_KEY: health_tip._today()}
        with capture_pushes() as sent:
            self.assertFalse(health_tip.send_tip(state))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
