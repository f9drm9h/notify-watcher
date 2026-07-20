"""Tests for the daily habit tracker (notify_watcher.topics.habits).

Pure stdlib unittest, no network: pushes are captured via tests/_util
(or a stub returning a message id when the pending-ack path is under test),
and the Discord reaction helpers are patched at the module seam.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

import requests

from notify_watcher.topics import habits
from tests._util import capture_pushes

UTC = _dt.timezone.utc
DAY = _dt.date(2026, 7, 20)


def _utc_for_local(hour: int, minute: int = 0, day: _dt.date = DAY) -> _dt.datetime:
    """The UTC instant whose DR (UTC-4) local wall clock reads day hour:minute."""
    local = _dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return local + _dt.timedelta(hours=4)


WATER = {
    "name": "water",
    "title": "Water - start the day hydrated",
    "tag": "droplet",
    "enabled": True,
    "times": ["05:30"],
    "messages": ["a", "b", "c", "d", "e"],
}

SLEEP = {
    "name": "sleep",
    "title": "Sleep - wind down",
    "tag": "zzz",
    "enabled": True,
    "priority": "high",
    "times": ["21:00", "22:00"],
    "messages": ["wind down", "go to bed"],
}


class TimesTest(unittest.TestCase):
    def test_sorts_dedups_and_drops_invalid(self):
        t = habits._times({"times": ["21:00", "05:30", "05:30", "24:00", "9:00",
                                     "09:60", 900, True, "18:00"]})
        self.assertEqual(t, ["05:30", "18:00", "21:00"])

    def test_missing_times_is_empty(self):
        self.assertEqual(habits._times({}), [])
        self.assertEqual(habits._times({"times": "18:00"}), [])


class LocalTimeTest(unittest.TestCase):
    def test_utc_to_dr_local(self):
        self.assertEqual(
            habits._local_now(_utc_for_local(5, 30)).strftime("%H:%M"), "05:30")

    def test_offset_clamped_and_defaulted(self):
        for raw, want in ((-4, -4), (99, 14), (-99, -12), ("x", -4),
                          (True, -4), (None, -4)):
            with mock.patch.object(habits, "_load_file",
                                   return_value={"utc_offset_hours": raw}):
                self.assertEqual(habits._utc_offset(), want, raw)

    def test_missing_file_uses_default_offset(self):
        with mock.patch.object(habits, "_load_file", return_value={}):
            self.assertEqual(habits._utc_offset(), habits.DEFAULT_UTC_OFFSET)


class SlotLogicTest(unittest.TestCase):
    def test_due_at_the_exact_minute(self):
        local = _dt.datetime(2026, 7, 20, 5, 30)
        self.assertEqual(habits._due_slots(local, ["05:30"], set()), ["05:30"])

    def test_not_due_one_minute_early(self):
        local = _dt.datetime(2026, 7, 20, 5, 29)
        self.assertEqual(habits._due_slots(local, ["05:30"], set()), [])

    def test_already_sent_excluded(self):
        local = _dt.datetime(2026, 7, 20, 22, 5)
        sent = {habits._slot_key(DAY, "21:00")}
        self.assertEqual(habits._due_slots(local, ["21:00", "22:00"], sent),
                         ["22:00"])

    def test_dropped_runs_leave_multiple_due(self):
        local = _dt.datetime(2026, 7, 20, 22, 30)
        self.assertEqual(habits._due_slots(local, ["21:00", "22:00"], set()),
                         ["21:00", "22:00"])

    def test_state_key_matches_legacy_water_key(self):
        # Migration guard: water keeps its historical state key.
        self.assertEqual(habits._state_key("water"), "water_slots_sent")

    def test_slot_key_carries_local_date_and_minute(self):
        self.assertEqual(habits._slot_key(DAY, "05:30"), "2026-07-20|05:30")


class RunOneTest(unittest.TestCase):
    def test_sends_one_push_and_records(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, WATER, _utc_for_local(5, 30))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Water - start the day hydrated")
        self.assertEqual(sent[0]["tags"], "droplet")
        self.assertEqual(sent[0]["topic"], "habits")
        self.assertEqual(sent[0]["priority"], "default")
        self.assertIn("2026-07-20|05:30", state["water_slots_sent"])

    def test_reminder_carries_a_done_button(self):
        with capture_pushes() as sent:
            habits._run_one({}, WATER, _utc_for_local(5, 30))
        self.assertEqual(sent[0]["actions"],
                         [{"label": "Done", "command": "DONE:water"}])

    def test_habit_priority_is_forwarded(self):
        with capture_pushes() as sent:
            habits._run_one({}, SLEEP, _utc_for_local(21, 0))
        self.assertEqual(sent[0]["priority"], "high")

    def test_records_event_log_history(self):
        # The dashboard and weekly life dashboard read habit history from the
        # event log; the direct-push path must keep feeding it.
        with capture_pushes():
            state = habits._run_one({}, WATER, _utc_for_local(5, 30))
        entries = [e for e in state["event_log"] if e["topic"] == "habits"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "water")
        self.assertEqual(entries[0]["action"], "push")

    def test_idempotent_within_a_slot(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, WATER, _utc_for_local(5, 30))
            habits._run_one(state, WATER, _utc_for_local(5, 45))
        self.assertEqual(len(sent), 1)

    def test_dropped_runs_send_latest_only(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, SLEEP, _utc_for_local(22, 30))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["message"], habits._message_for(
            SLEEP["messages"], DAY, "22:00"))
        self.assertEqual(len(state["sleep_slots_sent"]), 2)  # both marked, no burst

    def test_sleep_slot_past_utc_midnight_stays_on_the_local_day(self):
        # 21:05 local on Jul 20 is 01:05 UTC on Jul 21; the slot must key on
        # the LOCAL date so the dedup and daily reset stay on the habit day.
        with capture_pushes() as sent:
            state = habits._run_one({}, SLEEP, _utc_for_local(21, 5))
        self.assertEqual(len(sent), 1)
        self.assertIn("2026-07-20|21:00", state["sleep_slots_sent"])

    def test_no_push_before_first_slot_and_prunes(self):
        stale = [habits._slot_key(_dt.date(2026, 7, 19), "05:30"),
                 "2026-07-19|12"]  # a leftover legacy UTC-hour key too
        with capture_pushes() as sent:
            state = habits._run_one({"water_slots_sent": stale}, WATER,
                                    _utc_for_local(5, 0))
        self.assertEqual(sent, [])
        self.assertEqual(state["water_slots_sent"], [])  # yesterday's keys pruned

    def test_disabled_habit_is_skipped(self):
        with capture_pushes() as sent:
            state = habits._run_one({}, dict(WATER, enabled=False),
                                    _utc_for_local(5, 30))
        self.assertEqual(sent, [])
        self.assertNotIn("water_slots_sent", state)

    def test_malformed_habit_is_skipped(self):
        with capture_pushes() as sent:
            habits._run_one({}, {"name": "x", "times": ["05:30"]},
                            _utc_for_local(5, 30))  # no messages
            habits._run_one({}, {"name": "y", "messages": ["m"]},
                            _utc_for_local(5, 30))  # no times
        self.assertEqual(sent, [])

    def test_mute_skips_the_push_but_marks_the_slot(self):
        # until_active reads the real clock, so pin the mute far in the future.
        state = {"muted": {"habits": "2999-01-01T00:00:00+00:00"}}
        with capture_pushes() as sent:
            state = habits._run_one(state, WATER, _utc_for_local(5, 30))
        self.assertEqual(sent, [])  # silenced...
        self.assertIn("2026-07-20|05:30", state["water_slots_sent"])  # ...not deferred


class PendingRegistrationTest(unittest.TestCase):
    """The push returns a Discord message id -> it becomes a pending ack."""

    def _run(self, message_id):
        state: dict = {}
        with mock.patch.dict("os.environ", {"CHANNEL_HABITS": "chan1"}), \
                mock.patch.object(habits.ntfy, "push", return_value=message_id), \
                mock.patch.object(habits.discord_delivery, "add_reaction") as react:
            habits._run_one(state, WATER, _utc_for_local(5, 30))
        return state, react

    def test_message_id_registers_pending_and_seeds_reaction(self):
        state, react = self._run("9001")
        entry = state[habits.PENDING_KEY]["9001"]
        self.assertEqual(entry["habit"], "water")
        self.assertEqual(entry["channel"], "chan1")
        self.assertEqual(entry["sent"], _utc_for_local(5, 30).isoformat())
        react.assert_called_once_with("chan1", "9001", habits.ACK_EMOJI)

    def test_no_message_id_registers_nothing(self):
        state, react = self._run(None)
        self.assertNotIn(habits.PENDING_KEY, state)
        react.assert_not_called()

    def test_reaction_seed_failure_is_cosmetic(self):
        state: dict = {}
        with mock.patch.dict("os.environ", {"CHANNEL_HABITS": "chan1"}), \
                mock.patch.object(habits.ntfy, "push", return_value="9002"), \
                mock.patch.object(habits.discord_delivery, "add_reaction",
                                  side_effect=OSError("boom")):
            habits._run_one(state, WATER, _utc_for_local(5, 30))
        self.assertIn("9002", state[habits.PENDING_KEY])  # still polled for acks

    def test_pending_registry_is_capped(self):
        state = {habits.PENDING_KEY: {
            str(i): {"habit": "old", "channel": "c", "sent": "2026-07-20T00:00:00"}
            for i in range(habits.MAX_PENDING)
        }}
        with mock.patch.dict("os.environ", {"CHANNEL_HABITS": "chan1"}), \
                mock.patch.object(habits.ntfy, "push", return_value="new"), \
                mock.patch.object(habits.discord_delivery, "add_reaction"):
            habits._run_one(state, WATER, _utc_for_local(5, 30))
        pending = state[habits.PENDING_KEY]
        self.assertEqual(len(pending), habits.MAX_PENDING)
        self.assertIn("new", pending)
        self.assertNotIn("0", pending)  # oldest evicted


class MarkCompleteTest(unittest.TestCase):
    def test_logs_local_date_and_utc_timestamp(self):
        now = _utc_for_local(18, 10)
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            self.assertTrue(habits.mark_complete(state, "water", now=now))
        self.assertEqual(state[habits.LOG_KEY], [{
            "habit": "water",
            "date": "2026-07-20",
            "completed_at": now.isoformat(),
        }])

    def test_one_completion_per_local_day(self):
        now = _utc_for_local(18, 10)
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            self.assertTrue(habits.mark_complete(state, "water", now=now))
            self.assertFalse(habits.mark_complete(state, "water", now=now))
        self.assertEqual(len(state[habits.LOG_KEY]), 1)

    def test_next_local_day_logs_again(self):
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            habits.mark_complete(state, "water", now=_utc_for_local(6, 0))
            habits.mark_complete(
                state, "water",
                now=_utc_for_local(6, 0, day=_dt.date(2026, 7, 21)))
        self.assertEqual(len(state[habits.LOG_KEY]), 2)

    def test_suppresses_every_remaining_slot_today(self):
        # Acking the 21:00 sleep reminder also cancels the 22:00 one.
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[SLEEP]):
            habits.mark_complete(state, "sleep", now=_utc_for_local(21, 10))
        self.assertEqual(state["sleep_slots_sent"],
                         ["2026-07-20|21:00", "2026-07-20|22:00"])
        with capture_pushes() as sent:
            habits._run_one(state, SLEEP, _utc_for_local(22, 5))
        self.assertEqual(sent, [])  # the 22:00 reminder never fires

    def test_clears_only_that_habits_pending_acks(self):
        state = {habits.PENDING_KEY: {
            "1": {"habit": "water", "channel": "c", "sent": "x"},
            "2": {"habit": "sleep", "channel": "c", "sent": "x"},
        }}
        with mock.patch.object(habits, "_load", return_value=[WATER, SLEEP]):
            habits.mark_complete(state, "water", now=_utc_for_local(6, 0))
        self.assertEqual(list(state[habits.PENDING_KEY]), ["2"])

    def test_habit_gone_from_config_still_logs(self):
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[]):
            self.assertTrue(habits.mark_complete(state, "retired",
                                                 now=_utc_for_local(6, 0)))
        self.assertEqual(len(state[habits.LOG_KEY]), 1)

    def test_log_is_capped(self):
        state = {habits.LOG_KEY: [
            {"habit": "old", "date": f"2020-01-{(i % 28) + 1:02d}",
             "completed_at": "x"}
            for i in range(habits.MAX_LOG)
        ]}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            habits.mark_complete(state, "water", now=_utc_for_local(6, 0))
        self.assertEqual(len(state[habits.LOG_KEY]), habits.MAX_LOG)
        self.assertEqual(state[habits.LOG_KEY][-1]["habit"], "water")


class AckedTest(unittest.TestCase):
    def test_no_message_or_reactions_is_not_acked(self):
        self.assertFalse(habits._acked(None))
        self.assertFalse(habits._acked({}))
        self.assertFalse(habits._acked({"reactions": []}))

    def test_only_the_bots_own_seed_is_not_acked(self):
        msg = {"reactions": [{"emoji": {"name": "✅"}, "count": 1, "me": True}]}
        self.assertFalse(habits._acked(msg))

    def test_tap_on_the_seeded_reaction_is_acked(self):
        msg = {"reactions": [{"emoji": {"name": "✅"}, "count": 2, "me": True}]}
        self.assertTrue(habits._acked(msg))

    def test_any_other_user_reaction_is_acked(self):
        msg = {"reactions": [{"emoji": {"name": "💪"}, "count": 1, "me": False}]}
        self.assertTrue(habits._acked(msg))

    def test_malformed_reactions_are_ignored(self):
        msg = {"reactions": ["junk", {"count": "x", "me": False}, None]}
        self.assertFalse(habits._acked(msg))


class PollAcksTest(unittest.TestCase):
    NOW = _utc_for_local(9, 0)

    def _pending(self, habit="water", sent=None):
        return {"habit": habit, "channel": "chan1",
                "sent": (sent or _utc_for_local(5, 30)).isoformat()}

    def test_no_token_skips_polling_and_keeps_entries(self):
        state = {habits.PENDING_KEY: {"1": self._pending()}}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": ""}), \
                mock.patch.object(habits.discord_delivery, "get_message") as get:
            habits._poll_acks(state, self.NOW)
        get.assert_not_called()
        self.assertIn("1", state[habits.PENDING_KEY])

    def test_acked_message_logs_completion(self):
        state = {habits.PENDING_KEY: {"1": self._pending()}}
        acked = {"reactions": [{"count": 2, "me": True}]}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message",
                                  return_value=acked), \
                mock.patch.object(habits, "_load", return_value=[WATER]):
            habits._poll_acks(state, self.NOW)
        self.assertEqual(state[habits.LOG_KEY][0]["habit"], "water")
        self.assertEqual(state[habits.PENDING_KEY], {})

    def test_unacked_message_stays_pending(self):
        state = {habits.PENDING_KEY: {"1": self._pending()}}
        seed_only = {"reactions": [{"count": 1, "me": True}]}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message",
                                  return_value=seed_only):
            habits._poll_acks(state, self.NOW)
        self.assertIn("1", state[habits.PENDING_KEY])
        self.assertNotIn(habits.LOG_KEY, state)

    def test_deleted_message_is_dropped(self):
        state = {habits.PENDING_KEY: {"1": self._pending()}}
        err = requests.HTTPError(response=mock.Mock(status_code=404))
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message",
                                  side_effect=err):
            habits._poll_acks(state, self.NOW)
        self.assertEqual(state[habits.PENDING_KEY], {})

    def test_transient_error_keeps_the_entry_for_retry(self):
        state = {habits.PENDING_KEY: {"1": self._pending()}}
        err = requests.HTTPError(response=mock.Mock(status_code=500))
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message",
                                  side_effect=err):
            habits._poll_acks(state, self.NOW)
        self.assertIn("1", state[habits.PENDING_KEY])

    def test_network_error_keeps_the_entry_for_retry(self):
        state = {habits.PENDING_KEY: {"1": self._pending()}}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message",
                                  side_effect=OSError("boom")):
            habits._poll_acks(state, self.NOW)
        self.assertIn("1", state[habits.PENDING_KEY])

    def test_expired_entry_is_dropped_without_a_network_call(self):
        old = self.NOW - _dt.timedelta(hours=habits.PENDING_TTL_HOURS + 1)
        state = {habits.PENDING_KEY: {"1": self._pending(sent=old)}}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message") as get:
            habits._poll_acks(state, self.NOW)
        get.assert_not_called()
        self.assertEqual(state[habits.PENDING_KEY], {})

    def test_malformed_entry_is_dropped(self):
        state = {habits.PENDING_KEY: {"1": "junk", "2": {"habit": "w"}}}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message") as get:
            habits._poll_acks(state, self.NOW)
        get.assert_not_called()  # "junk" dropped; "2" has no parseable sent -> dropped
        self.assertEqual(state[habits.PENDING_KEY], {})


class RunTest(unittest.TestCase):
    def test_isolates_a_failing_habit(self):
        with mock.patch.object(habits, "_load", return_value=[{"bad": 1}, WATER]), \
                mock.patch.object(habits, "_utcnow",
                                  return_value=_utc_for_local(5, 30)), \
                capture_pushes() as sent:
            habits.run({})
        self.assertEqual(len(sent), 1)  # the good habit still fired

    def test_shared_evening_slot_fires_every_habit(self):
        evening = [dict(WATER, name=n, title=n.capitalize(), times=["18:00"])
                   for n in ("exercise", "meditate", "cleaning")]
        with mock.patch.object(habits, "_load", return_value=evening), \
                mock.patch.object(habits, "_utcnow",
                                  return_value=_utc_for_local(18, 0)), \
                capture_pushes() as sent:
            habits.run({})
        self.assertEqual({s["title"] for s in sent},
                         {"Exercise", "Meditate", "Cleaning"})

    def test_ack_poll_failure_never_blocks_reminders(self):
        with mock.patch.object(habits, "_poll_acks",
                               side_effect=RuntimeError("x")), \
                mock.patch.object(habits, "_load", return_value=[WATER]), \
                mock.patch.object(habits, "_utcnow",
                                  return_value=_utc_for_local(5, 30)), \
                capture_pushes() as sent:
            habits.run({})
        self.assertEqual(len(sent), 1)

    def test_poll_runs_before_sends_so_a_fresh_ack_silences_them(self):
        # Sleep was acked at 21:50 (message reacted); the 22:00 run must poll
        # first and therefore send nothing.
        state = {habits.PENDING_KEY: {"1": {
            "habit": "sleep", "channel": "chan1",
            "sent": _utc_for_local(21, 0).isoformat()}}}
        acked = {"reactions": [{"count": 2, "me": True}]}
        with mock.patch.dict("os.environ", {"DISCORD_TOKEN": "t"}), \
                mock.patch.object(habits.discord_delivery, "get_message",
                                  return_value=acked), \
                mock.patch.object(habits, "_load", return_value=[SLEEP]), \
                mock.patch.object(habits, "_utcnow",
                                  return_value=_utc_for_local(22, 1)), \
                capture_pushes() as sent:
            state = habits.run(state)
        self.assertEqual(sent, [])
        self.assertEqual(state[habits.LOG_KEY][0]["habit"], "sleep")


class ShippedConfigTest(unittest.TestCase):
    def test_habits_json_matches_the_requested_schedule(self):
        loaded = habits._load()
        by_name = {h.get("name"): h for h in loaded}
        self.assertEqual(
            set(by_name),
            {"water", "exercise", "meditate", "cleaning", "sleep"})
        self.assertEqual(habits._times(by_name["water"]), ["05:30"])
        self.assertEqual(habits._times(by_name["exercise"]), ["18:00"])
        self.assertEqual(habits._times(by_name["meditate"]), ["18:00"])
        self.assertEqual(habits._times(by_name["cleaning"]), ["18:00"])
        self.assertEqual(habits._times(by_name["sleep"]), ["21:00", "22:00"])
        for habit in loaded:
            self.assertTrue(habit.get("enabled"), habit["name"])
            self.assertTrue(all(isinstance(m, str) and m
                                for m in habit["messages"]), habit["name"])

    def test_offset_is_dominican_republic(self):
        self.assertEqual(habits._utc_offset(), -4)


if __name__ == "__main__":
    unittest.main()
