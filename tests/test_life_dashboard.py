"""Tests for the weekly life dashboard topic (notify_watcher.topics.life_dashboard)."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from notify_watcher.topics import life_dashboard as ld
from tests._util import capture_pushes

# Sunday, 2026-06-14, the last day of ISO week 2026-W24 (Mon 2026-06-08 = W24).
SUNDAY = dt.datetime(2026, 6, 14, 12, 30, tzinfo=dt.timezone.utc)
SATURDAY = dt.datetime(2026, 6, 13, 12, 30, tzinfo=dt.timezone.utc)


def _entry(days_ago: float, topic="movies", action="push", score=50,
           title="T", source="", detail=""):
    ts = (SUNDAY - dt.timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "topic": topic, "action": action, "score": score,
            "title": title, "source": source, "detail": detail}


class WindowTest(unittest.TestCase):
    def test_keeps_only_the_trailing_week(self):
        log = [_entry(1), _entry(6.5), _entry(8), _entry(30)]
        self.assertEqual(len(ld._window(log, SUNDAY)), 2)

    def test_bad_entries_and_timestamps_are_skipped(self):
        log = ["junk", {"ts": "not-a-date"}, {}, _entry(2)]
        self.assertEqual(len(ld._window(log, SUNDAY)), 1)


class SectionTest(unittest.TestCase):
    def test_habits_counts_nudges_per_name(self):
        entries = [
            _entry(1, topic="habits", source="water"),
            _entry(2, topic="habits", source="water"),
            _entry(3, topic="habits", source="stretch"),
            _entry(1, topic="movies"),  # ignored
        ]
        out = ld._section_habits(entries)
        self.assertIn("💪 HABITS", out)
        self.assertIn("water (2)", out)
        self.assertIn("stretch (1)", out)

    def test_habits_skipped_when_none(self):
        self.assertIsNone(ld._section_habits([_entry(1, topic="movies")]))

    def test_fx_line_reports_weekly_move(self):
        state = {"fx_last_rate": 60.80, "fx_week_baseline": {"week": "2026-W24", "rate": 60.10}}
        line = ld._fx_line(state)
        self.assertIn("60.10 -> 60.80", line)
        self.assertIn("+0.70", line)

    def test_fx_line_steady(self):
        state = {"fx_last_rate": 60.10, "fx_week_baseline": {"rate": 60.10}}
        self.assertIn("held steady", ld._fx_line(state))

    def test_fx_line_none_when_untracked(self):
        self.assertIsNone(ld._fx_line({}))
        self.assertIsNone(ld._fx_line({"fx_last_rate": 60.1}))  # no baseline

    def test_upcoming_bills_filters_to_window(self):
        today = dt.date(2026, 6, 14)
        bills = [
            {"name": "Rent", "due_day": 30},          # 16 days away -> out
            {"name": "Internet bill", "due_day": 22},  # 8 days away -> out
            {"name": "Water", "due_day": 18},          # 4 days away -> in
        ]
        lines = ld._upcoming_bills(today, bills=bills)
        self.assertEqual(len(lines), 1)
        self.assertIn("Water due 2026-06-18 (in 4 days)", lines[0])

    def test_upcoming_bills_handles_tomorrow_and_bad_rows(self):
        today = dt.date(2026, 6, 14)
        bills = [{"name": "X", "due_day": 15}, {"name": "", "due_day": 16},
                 {"due_day": "oops"}]
        lines = ld._upcoming_bills(today, bills=bills)
        self.assertEqual(lines, ["X due 2026-06-15 (tomorrow)"])

    def test_finance_combines_available_lines(self):
        state = {"fx_last_rate": 60.80, "fx_week_baseline": {"rate": 60.10}}
        entries = [_entry(1, topic="spending", title="Weekly spending summary",
                          detail="Spent RD$4,210 last week (-12%)\nTop: groceries")]
        with mock.patch.object(ld, "_upcoming_bills", return_value=["Rent due soon"]):
            out = ld._section_finance(state, entries, dt.date(2026, 6, 14))
        self.assertIn("💰 FINANCE", out)
        self.assertIn("Spending: Spent RD$4,210 last week (-12%)", out)
        self.assertIn("USD/DOP", out)
        self.assertIn("Upcoming bills: Rent due soon", out)

    def test_finance_none_when_empty(self):
        with mock.patch.object(ld, "_upcoming_bills", return_value=[]):
            self.assertIsNone(ld._section_finance({}, [], dt.date(2026, 6, 14)))

    def test_weather_section_counts_and_names_latest(self):
        entries = [
            _entry(1, topic="weather", title="Storm watch"),
            _entry(3, topic="onamet", title="Hurricane watch issued"),
            _entry(2, topic="uv", title="High UV index"),
        ]
        out = ld._section_weather(entries)
        self.assertIn("🌤 WEATHER & ENVIRONMENT", out)
        self.assertIn("Weather alerts logged: 2", out)
        self.assertIn("latest: Storm watch", out)  # newest of the weather group
        self.assertIn("UV / air-quality warnings: 1", out)

    def test_entertainment_top_release_and_news(self):
        entries = [
            _entry(1, topic="games", score=78, title="Silksong dated"),
            _entry(2, topic="movies", score=40, title="Trailer"),
            _entry(3, topic="golden_sun", title="Remaster rumor"),
            _entry(1, topic="anthropic_news", title="New model"),
        ]
        out = ld._section_entertainment(entries)
        self.assertIn("Top release: [78] Silksong dated", out)
        self.assertIn('Golden Sun: 1 item(s); latest: "Remaster rumor"', out)
        self.assertIn('AI news: 1 item(s); latest: "New model"', out)

    def test_system_health_errors_and_push_count(self):
        entries = [_entry(1, action="push"), _entry(2, action="push"),
                   _entry(3, action="digest")]
        health = {
            "fx": {"last_ok": SUNDAY.isoformat()},
            "energy": {"last_error": "boom", "last_error_ts": (SUNDAY - dt.timedelta(days=1)).isoformat()},
            "fuel": {"last_error": "stale", "last_error_ts": (SUNDAY - dt.timedelta(days=20)).isoformat()},
        }
        out = ld._section_system(entries, health, SUNDAY)
        self.assertIn("Topics with errors this week: energy", out)
        self.assertNotIn("fuel", out)  # error too old to be "this week"
        self.assertIn("2 notification(s) pushed this week", out)


class RunTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._now = mock.patch.object(ld, "_utcnow", return_value=SUNDAY)
        self._now.start()
        self.addCleanup(self._now.stop)

    def test_sends_once_per_week_on_sunday(self):
        state = {"event_log": [_entry(1, action="push", score=70, title="Big",
                                      topic="games")]}
        with capture_pushes() as sent:
            state = ld.run(state)
            state = ld.run(state)  # same Sunday again -> deduped
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Your week in review")
        self.assertIn("🎬 ENTERTAINMENT & NEWS", sent[0]["message"])
        self.assertEqual(state[ld.STATE_KEY], "2026-W24")

    def test_empty_week_is_silent_but_stamps(self):
        with capture_pushes() as sent:
            state = ld.run({})
        self.assertEqual(sent, [])
        self.assertEqual(state[ld.STATE_KEY], "2026-W24")

    def test_not_sunday_is_a_noop(self):
        with mock.patch.object(ld, "_utcnow", return_value=SATURDAY), \
             capture_pushes() as sent:
            state = ld.run({"event_log": [_entry(1)]})
        self.assertEqual(sent, [])
        self.assertNotIn(ld.STATE_KEY, state)

    def test_not_daily_run_is_a_noop(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}), \
             capture_pushes() as sent:
            state = ld.run({"event_log": [_entry(1)]})
        self.assertEqual(sent, [])
        self.assertNotIn(ld.STATE_KEY, state)

    def test_partial_data_still_pushes_with_present_sections(self):
        # Only system-health data (pushes) — other sections skip gracefully.
        state = {"event_log": [_entry(1, action="push", topic="iss", title="ISS pass")]}
        with capture_pushes() as sent:
            ld.run(state)
        self.assertEqual(len(sent), 1)
        msg = sent[0]["message"]
        self.assertIn("⚙️ SYSTEM HEALTH", msg)
        self.assertNotIn("💪 HABITS", msg)


if __name__ == "__main__":
    unittest.main()
