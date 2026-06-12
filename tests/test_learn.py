"""Tests for the consolidated daily learning push (notify_watcher.topics.learn)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import learn
from tests._util import capture_pushes

# A trimmed-down sample of the Wikimedia featured feed shape.
SAMPLE_FEED = {
    "tfa": {
        "normalizedtitle": "Apollo 11",
        "extract": "Apollo 11 was the spaceflight that first landed humans on the Moon. " * 6,
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Apollo_11"}},
    },
    "onthisday": [
        {"text": "An important event happened.", "year": 1969},
        {"text": "Another event.", "year": 1500},
    ],
}


class ParseTest(unittest.TestCase):
    def test_on_this_day_line_is_deterministic(self):
        d = _dt.date(2026, 7, 20)
        line = learn._on_this_day_line(SAMPLE_FEED, d)
        self.assertEqual(line, learn._on_this_day_line(SAMPLE_FEED, d))
        self.assertRegex(line, r"^\d+: ")  # "YEAR: text"

    def test_on_this_day_empty_feed(self):
        self.assertEqual(learn._on_this_day_line({}, _dt.date(2026, 1, 1)), "")

    def test_featured_extracts_fields_and_truncates(self):
        title, extract, url = learn._featured(SAMPLE_FEED)
        self.assertEqual(title, "Apollo 11")
        self.assertEqual(url, "https://en.wikipedia.org/wiki/Apollo_11")
        self.assertLessEqual(len(extract), learn._MAX_EXTRACT + 3)  # +"..."
        self.assertTrue(extract.endswith("..."))

    def test_featured_missing_tfa(self):
        self.assertEqual(learn._featured({}), ("", "", None))

    def test_compose_skips_empty_bodies(self):
        msg = learn._compose([("A", "body a"), ("B", ""), ("C", "body c")])
        self.assertIn("A\nbody a", msg)
        self.assertIn("C\nbody c", msg)
        self.assertNotIn("B", msg)

    def test_curated_fact_returns_label_and_text(self):
        # Force the verbatim path (no LLM) for determinism.
        with mock.patch.object(learn.summarize, "one_line", return_value=None):
            label, fact = learn._curated_fact(_dt.date(2026, 3, 1))
        self.assertIn(label, [c[0] for c in learn.CHANNELS])
        self.assertTrue(fact)


class ChannelsTest(unittest.TestCase):
    """Golden test over the real data/ channel files: every channel listed in
    learn.CHANNELS must load and contain only well-formed {text, src} entries,
    so a malformed curated file fails CI instead of silently emptying a day's
    fact section."""

    def test_every_channel_file_loads_with_valid_entries(self):
        from notify_watcher import kb

        for label, filename in learn.CHANNELS:
            with self.subTest(channel=label):
                items = kb.load(kb.DATA_DIR / filename)
                self.assertTrue(items, f"{filename} is empty or failed to load")
                for item in items:
                    self.assertTrue(str(item.get("text", "")).strip(),
                                    f"{filename} has an entry without text")
                    self.assertTrue(str(item.get("src", "")).strip(),
                                    f"{filename} has an entry without src")


class RunTest(unittest.TestCase):
    """The consolidated daily push. The standalone knowledge push that run()
    also fires is neutralized here and covered by tests/test_learn_knowledge.py."""

    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._knowledge = mock.patch.object(
            learn, "_run_knowledge", side_effect=lambda state, now=None: state)
        self._knowledge.start()
        self.addCleanup(self._knowledge.stop)

    def test_run_sends_one_consolidated_push_and_stamps(self):
        with mock.patch.object(learn, "_fetch_feed", return_value=SAMPLE_FEED), \
             mock.patch.object(learn.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            state = learn.run({})
        self.assertEqual(len(sent), 1)  # ONE consolidated push, not three
        self.assertEqual(sent[0]["title"], "Daily learning")
        self.assertIn("On this day", sent[0]["message"])
        self.assertIn("Featured: Apollo 11", sent[0]["message"])
        self.assertEqual(sent[0]["click_url"], "https://en.wikipedia.org/wiki/Apollo_11")
        self.assertEqual(state[learn.STATE_KEY], _dt.date.today().isoformat())

    def test_run_is_idempotent_per_day(self):
        with mock.patch.object(learn, "_fetch_feed", return_value=SAMPLE_FEED), \
             mock.patch.object(learn.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            state = learn.run({})
            learn.run(state)  # second run same day
        self.assertEqual(len(sent), 1)

    def test_run_degrades_when_feed_unavailable(self):
        # Wikimedia down -> still send the curated fact section.
        with mock.patch.object(learn, "_fetch_feed", side_effect=RuntimeError("boom")), \
             mock.patch.object(learn.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            learn.run({})
        self.assertEqual(len(sent), 1)
        self.assertNotIn("Featured:", sent[0]["message"])

    def test_run_skips_when_not_daily(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = learn.run({})
        self.assertEqual(sent, [])
        self.assertNotIn(learn.STATE_KEY, state)


if __name__ == "__main__":
    unittest.main()
