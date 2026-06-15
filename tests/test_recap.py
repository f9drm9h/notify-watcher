"""Tests for the weekly recap topic (notify_watcher.topics.recap)."""
from __future__ import annotations

import datetime as dt
import types
import unittest
from unittest import mock

from notify_watcher.topics import recap
from tests._util import capture_pushes

NOW = dt.datetime(2026, 6, 8, 12, 30, tzinfo=dt.timezone.utc)  # Monday, 2026-W24


class _FrozenDateTime(dt.datetime):
    """datetime whose now() is pinned to NOW.

    recap.run reads the wall clock (recap._dt.datetime.now) and recap._window
    keeps only the trailing week, so a test fixture anchored to NOW silently
    ages out of the window once real time passes it. Freezing now() makes
    RunTest deterministic without touching production behavior.
    """

    @classmethod
    def now(cls, tz=None):
        return NOW


def _entry(days_ago: float, topic="movies", action="push", score=50, title="T"):
    ts = (NOW - dt.timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "topic": topic, "action": action, "score": score, "title": title}


class WindowTest(unittest.TestCase):
    def test_keeps_only_the_trailing_week(self):
        log = [_entry(1), _entry(6.5), _entry(8), _entry(30)]
        self.assertEqual(len(recap._window(log, NOW)), 2)

    def test_bad_entries_and_timestamps_are_skipped(self):
        log = ["junk", {"ts": "not-a-date"}, {}, _entry(2)]
        self.assertEqual(len(recap._window(log, NOW)), 1)


class SummarizeTest(unittest.TestCase):
    def test_counts_busiest_and_top_story(self):
        entries = [
            _entry(1, topic="movies", action="digest", score=33),
            _entry(2, topic="movies", action="push", score=62, title="Trailer out"),
            _entry(3, topic="fda", action="push", score=78, title="New approval"),
            _entry(4, topic="movies", action="drop", score=10),
        ]
        body = recap._summarize(entries, {"fx": {"last_ok": "2026-06-08"}})
        self.assertIn("2 live pushes, 1 digested, 1 dropped", body)
        self.assertIn("Busiest: movies (3), fda (1)", body)
        self.assertIn("Top story: [78] New approval", body)
        self.assertIn("All 1 topics healthy", body)

    def test_reading_list_count_line(self):
        body = recap._summarize([_entry(1)], {}, reading_list_count=6)
        self.assertIn("Reading list: 6 saved item(s)", body)
        # ...and an empty list adds no line at all.
        self.assertNotIn("Reading list", recap._summarize([_entry(1)], {}))

    def test_failing_topics_are_named(self):
        body = recap._summarize([_entry(1)], {
            "fx": {"last_ok": "x"},
            "energy": {"last_error": "boom"},
        })
        self.assertIn("Failing now: energy", body)
        self.assertNotIn("healthy", body)


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        # Freeze recap's clock to NOW so the trailing-week window is
        # deterministic. Scoped to the recap module's _dt reference so the
        # global datetime is untouched; timedelta/timezone stay real.
        frozen_dt = types.SimpleNamespace(
            datetime=_FrozenDateTime,
            timedelta=dt.timedelta,
            timezone=dt.timezone,
            date=dt.date,
        )
        self._clock = mock.patch.object(recap, "_dt", frozen_dt)
        self._clock.start()
        self.addCleanup(self._clock.stop)

    def test_sends_once_per_week(self):
        state = {"event_log": [_entry(1, action="push", score=70, title="Big")]}
        with capture_pushes() as sent:
            state = recap.run(state)
            state = recap.run(state)  # same week again
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Your week in notifications")
        self.assertIn("1 live pushes", sent[0]["message"])
        self.assertTrue(state[recap.STATE_KEY])

    def test_empty_log_skips_silently_but_stamps_the_week(self):
        with capture_pushes() as sent:
            state = recap.run({})
        self.assertEqual(sent, [])
        self.assertTrue(state[recap.STATE_KEY])

    def test_not_daily_run_is_a_noop(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}), \
             capture_pushes() as sent:
            state = recap.run({"event_log": [_entry(1)]})
        self.assertEqual(sent, [])
        self.assertNotIn(recap.STATE_KEY, state)


if __name__ == "__main__":
    unittest.main()
