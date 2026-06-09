"""Tests for the topic-selection filter in notify_watcher.main.

`NOTIFY_ONLY` lets a lightweight workflow (the 15-minute Twitch check) run a single
topic without invoking the full sweep. These pin the pure filter: blank -> all,
allowlist -> subset in declared order, unknown names ignored.
"""
from __future__ import annotations

import unittest

from notify_watcher import main


class SelectedTopicsTest(unittest.TestCase):
    def test_blank_returns_all_topics(self):
        self.assertEqual(main._selected_topics(""), main.TOPICS)
        self.assertEqual(main._selected_topics("   "), main.TOPICS)

    def test_single_topic_allowlist(self):
        sel = main._selected_topics("twitch")
        self.assertEqual([n for n, _ in sel], ["twitch"])

    def test_multiple_preserve_declared_order(self):
        # Order follows TOPICS, not the order given in the env var.
        sel = main._selected_topics("iss,twitch")
        names = [n for n, _ in sel]
        self.assertEqual(set(names), {"twitch", "iss"})
        declared = [n for n, _ in main.TOPICS]
        self.assertEqual(names, [n for n in declared if n in {"twitch", "iss"}])

    def test_unknown_names_ignored(self):
        self.assertEqual(main._selected_topics("nope,twitch,alsonope"),
                         [(n, r) for n, r in main.TOPICS if n == "twitch"])

    def test_runnable_topic_is_callable(self):
        sel = main._selected_topics("twitch")
        self.assertTrue(callable(sel[0][1]))


if __name__ == "__main__":
    unittest.main()
