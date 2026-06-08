"""Tests for the periodic drink-water reminders (notify_watcher.topics.water)."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import water
from tests._util import capture_pushes

UTC = _dt.timezone.utc


def _at(hour: int, day: _dt.date = _dt.date(2026, 7, 20)) -> _dt.datetime:
    return _dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


class SlotLogicTest(unittest.TestCase):
    def test_no_slot_due_before_first_hour(self):
        self.assertEqual(water._due_slots(_at(9), set()), [])

    def test_one_slot_due_at_its_hour(self):
        self.assertEqual(water._due_slots(_at(12), set()), [12])

    def test_already_sent_slot_excluded(self):
        sent = {water._slot_key(_dt.date(2026, 7, 20), 12)}
        self.assertEqual(water._due_slots(_at(15), sent), [15])

    def test_dropped_runs_leave_multiple_due(self):
        # Runner only fires at 21:00 after earlier runs were skipped.
        self.assertEqual(water._due_slots(_at(21), set()), [12, 15, 18, 21])

    def test_keys_are_per_day(self):
        k1 = water._slot_key(_dt.date(2026, 7, 20), 12)
        k2 = water._slot_key(_dt.date(2026, 7, 21), 12)
        self.assertNotEqual(k1, k2)


class MessageTest(unittest.TestCase):
    def test_message_is_deterministic(self):
        d = _dt.date(2026, 7, 20)
        self.assertEqual(water._message_for(d, 12), water._message_for(d, 12))

    def test_message_from_curated_set(self):
        d = _dt.date(2026, 7, 20)
        for h in water.REMINDER_UTC_HOURS:
            self.assertIn(water._message_for(d, h), water._MESSAGES)

    def test_adjacent_slots_differ(self):
        d = _dt.date(2026, 7, 20)
        msgs = {water._message_for(d, h) for h in water.REMINDER_UTC_HOURS}
        self.assertGreater(len(msgs), 1)


class RunTest(unittest.TestCase):
    def test_sends_one_push_at_a_slot_and_records_it(self):
        with mock.patch.object(water, "_utcnow", return_value=_at(12)), \
             capture_pushes() as sent:
            state = water.run({})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Drink water")
        self.assertIn(sent[0]["message"], water._MESSAGES)
        self.assertIn(water._slot_key(_at(12).date(), 12), state[water.STATE_KEY])

    def test_idempotent_within_a_slot(self):
        with mock.patch.object(water, "_utcnow", return_value=_at(12)), \
             capture_pushes() as sent:
            state = water.run({})
            water.run(state)  # same slot again -> no second push
        self.assertEqual(len(sent), 1)

    def test_fires_again_at_the_next_slot(self):
        with capture_pushes() as sent:
            with mock.patch.object(water, "_utcnow", return_value=_at(12)):
                state = water.run({})
            with mock.patch.object(water, "_utcnow", return_value=_at(15)):
                water.run(state)
        self.assertEqual(len(sent), 2)

    def test_no_push_before_first_slot(self):
        with mock.patch.object(water, "_utcnow", return_value=_at(9)), \
             capture_pushes() as sent:
            state = water.run({})
        self.assertEqual(sent, [])
        self.assertEqual(state[water.STATE_KEY], [])

    def test_dropped_runs_send_latest_only_no_burst(self):
        # First run of the day lands at 21:00; should send ONE push, not four,
        # and mark every earlier slot handled so they never backfill.
        with mock.patch.object(water, "_utcnow", return_value=_at(21)), \
             capture_pushes() as sent:
            state = water.run({})
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(state[water.STATE_KEY]), len(water.REMINDER_UTC_HOURS))

    def test_old_day_keys_pruned(self):
        stale = [water._slot_key(_dt.date(2026, 7, 19), h) for h in water.REMINDER_UTC_HOURS]
        with mock.patch.object(water, "_utcnow", return_value=_at(9)), \
             capture_pushes():
            state = water.run({water.STATE_KEY: stale})
        self.assertEqual(state[water.STATE_KEY], [])


if __name__ == "__main__":
    unittest.main()
