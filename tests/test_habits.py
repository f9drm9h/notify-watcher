"""Tests for the config-driven habit nudges (notify_watcher.topics.habits)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import habits
from tests._util import capture_pushes

UTC = _dt.timezone.utc


def _at(hour: int, day: _dt.date = _dt.date(2026, 7, 20)) -> _dt.datetime:
    return _dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


WATER = {
    "name": "water",
    "title": "Drink water",
    "tag": "droplet",
    "enabled": True,
    "hours": [12, 15, 18, 21],
    "messages": ["a", "b", "c", "d", "e"],
}


class HoursTest(unittest.TestCase):
    def test_sorts_dedups_and_drops_invalid(self):
        h = habits._hours({"hours": [21, 12, 12, 24, -1, "x", True, 15]})
        self.assertEqual(h, [12, 15, 21])  # 24/-1/"x"/True dropped, 12 deduped

    def test_missing_hours_is_empty(self):
        self.assertEqual(habits._hours({}), [])


class SlotLogicTest(unittest.TestCase):
    def test_one_slot_due_at_its_hour(self):
        self.assertEqual(habits._due_slots(_at(12), [12, 15, 18, 21], set()), [12])

    def test_already_sent_excluded(self):
        sent = {habits._slot_key(_dt.date(2026, 7, 20), 12)}
        self.assertEqual(habits._due_slots(_at(15), [12, 15, 18, 21], sent), [15])

    def test_dropped_runs_leave_multiple_due(self):
        self.assertEqual(habits._due_slots(_at(21), [12, 15, 18, 21], set()),
                         [12, 15, 18, 21])

    def test_state_key_matches_legacy_water_key(self):
        # Migration guard: water keeps its historical state key.
        self.assertEqual(habits._state_key("water"), "water_slots_sent")


class RunOneTest(unittest.TestCase):
    def test_sends_one_push_and_records(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, WATER, _at(12))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Drink water")
        self.assertEqual(sent[0]["tags"], "droplet")
        self.assertIn(habits._slot_key(_at(12).date(), 12), state["water_slots_sent"])

    def test_idempotent_within_a_slot(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, WATER, _at(12))
            habits._run_one(state, WATER, _at(12))
        self.assertEqual(len(sent), 1)

    def test_dropped_runs_send_latest_only(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, WATER, _at(21))
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(state["water_slots_sent"]), 4)  # all marked, no burst

    def test_no_push_before_first_slot_and_prunes(self):
        stale = [habits._slot_key(_dt.date(2026, 7, 19), 12)]
        with capture_pushes() as sent:
            state = habits._run_one({"water_slots_sent": stale}, WATER, _at(9))
        self.assertEqual(sent, [])
        self.assertEqual(state["water_slots_sent"], [])  # yesterday's key pruned

    def test_disabled_habit_is_skipped(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, dict(WATER, enabled=False), _at(12))
        self.assertEqual(sent, [])
        self.assertNotIn("water_slots_sent", state)

    def test_malformed_habit_is_skipped(self):
        with capture_pushes() as sent:
            habits._run_one({}, {"name": "x", "hours": [12]}, _at(12))  # no messages
            habits._run_one({}, {"name": "y", "messages": ["m"]}, _at(12))  # no hours
        self.assertEqual(sent, [])


class RunTest(unittest.TestCase):
    def test_isolates_a_failing_habit(self):
        good = WATER
        with mock.patch.object(habits, "_load", return_value=[{"bad": 1}, good]), \
             mock.patch.object(habits, "_utcnow", return_value=_at(12)), \
             capture_pushes() as sent:
            habits.run({})
        self.assertEqual(len(sent), 1)  # the good habit still fired

    def test_multiple_habits_each_fire(self):
        stand = dict(WATER, name="stand", title="Stand", hours=[12])
        with mock.patch.object(habits, "_load", return_value=[WATER, stand]), \
             mock.patch.object(habits, "_utcnow", return_value=_at(12)), \
             capture_pushes() as sent:
            habits.run({})
        self.assertEqual({s["title"] for s in sent}, {"Drink water", "Stand"})


class ShippedConfigTest(unittest.TestCase):
    def test_habits_json_loads_and_water_is_enabled(self):
        loaded = habits._load()
        by_name = {h.get("name"): h for h in loaded}
        self.assertIn("water", by_name)
        water = by_name["water"]
        self.assertTrue(water.get("enabled"))
        self.assertEqual(habits._hours(water), [12, 15, 18, 21])
        self.assertTrue(all(isinstance(m, str) and m for m in water["messages"]))


if __name__ == "__main__":
    unittest.main()
