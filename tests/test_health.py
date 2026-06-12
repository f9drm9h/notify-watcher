"""Tests for the topic health contract (notify_watcher.health).

A topic reports its source outcome (ok / source_failed, data_count, message)
into the transient ``state["_topic_status"]`` without crashing the run;
main.py consumes the report to stamp ``topic_health``. These pin the report
shape, the last_data side effect of an ok-with-data report, and consume().
"""
from __future__ import annotations

import unittest

from notify_watcher import health


class TopicStatusTest(unittest.TestCase):
    def test_ok_report_shape(self):
        state: dict = {}
        health.source_ok(state, "fuel", data_count=6)
        status = state[health.STATUS_KEY]["fuel"]
        self.assertTrue(status["ok"])
        self.assertFalse(status["source_failed"])
        self.assertEqual(status["data_count"], 6)

    def test_failed_report_shape(self):
        state: dict = {}
        health.source_failed(state, "fuel", "listing fetch failed: HTTP 403")
        status = state[health.STATUS_KEY]["fuel"]
        self.assertFalse(status["ok"])
        self.assertTrue(status["source_failed"])
        self.assertEqual(status["data_count"], 0)
        self.assertEqual(status["message"], "listing fetch failed: HTTP 403")

    def test_ok_with_data_stamps_last_data(self):
        state: dict = {}
        health.source_ok(state, "fuel", data_count=6)
        self.assertIn("last_data", state["topic_health"]["fuel"])

    def test_ok_with_zero_items_does_not_stamp_last_data(self):
        # An empty-but-healthy source (quiet CAP feed, no streamers live) must
        # not look like "data seen" to the watchdog's staleness check.
        state: dict = {}
        health.source_ok(state, "onamet", data_count=0)
        self.assertNotIn("topic_health", state)

    def test_failed_report_does_not_stamp_last_data(self):
        state: dict = {}
        health.source_failed(state, "fuel", "boom")
        self.assertNotIn("topic_health", state)

    def test_message_is_truncated(self):
        state: dict = {}
        health.source_failed(state, "fuel", "x" * 1000)
        self.assertEqual(len(state[health.STATUS_KEY]["fuel"]["message"]),
                         health._MESSAGE_LEN)

    def test_last_report_in_a_run_wins(self):
        state: dict = {}
        health.source_failed(state, "outages", "EDESUR down")
        health.source_ok(state, "outages", data_count=3)
        self.assertTrue(state[health.STATUS_KEY]["outages"]["ok"])

    def test_blank_topic_is_ignored(self):
        state: dict = {}
        health.source_ok(state, "", data_count=5)
        self.assertEqual(state, {})


class ConsumeTest(unittest.TestCase):
    def test_consume_pops_the_report(self):
        state: dict = {}
        health.source_failed(state, "fx", "boom")
        status = health.consume(state, "fx")
        self.assertTrue(status["source_failed"])
        self.assertIsNone(health.consume(state, "fx"))

    def test_consume_without_any_reports_is_none(self):
        self.assertIsNone(health.consume({}, "fx"))

    def test_consume_only_takes_the_named_topic(self):
        state: dict = {}
        health.source_ok(state, "fx", data_count=1)
        health.source_failed(state, "fuel", "boom")
        self.assertTrue(health.consume(state, "fx")["ok"])
        self.assertIn("fuel", state[health.STATUS_KEY])


class AdoptedRegistryTest(unittest.TestCase):
    def test_adopted_names_are_real_topics(self):
        # Every adopted name must exist in main.TOPICS, or main.py would treat
        # its reports as belonging to a topic that never runs.
        from notify_watcher import main
        declared = {name for name, _ in main.TOPICS}
        self.assertTrue(health.ADOPTED <= declared,
                        health.ADOPTED - declared)


if __name__ == "__main__":
    unittest.main()
