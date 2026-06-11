"""Tests for the reply-button control channel (notify_watcher.control).

Pure stdlib unittest, no network: the ntfy poll is exercised by patching
requests.get with a canned ndjson response, and the handlers are tested
against in-memory state dicts with injected clocks.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher import control, events
from notify_watcher.topics import habits
from tests._util import capture_pushes

UTC = _dt.timezone.utc
NOW = _dt.datetime(2026, 6, 10, 14, 30, tzinfo=UTC)

WATER = {
    "name": "water",
    "title": "Drink water",
    "enabled": True,
    "hours": [12, 15, 18, 21],
    "messages": ["m"],
}


def _env(**overrides):
    """Patch the control-channel env vars; unset everything not overridden."""
    base = {"NTFY_CONTROL_TOPIC": "", "NTFY_SERVER": ""}
    base.update(overrides)
    return mock.patch.dict("os.environ", base)


class PollTest(unittest.TestCase):
    def test_unset_topic_returns_empty_and_skips_network(self):
        with _env(), mock.patch.object(control.requests, "get") as get:
            self.assertEqual(control.poll({}), [])
        get.assert_not_called()

    def test_poll_collects_messages_and_advances_cursor(self):
        ndjson = "\n".join([
            '{"id": "id1", "event": "message", "message": "DONE:water"}',
            '{"id": "id2", "event": "keepalive"}',
            "not json at all",
            '{"id": "id3", "event": "message", "message": "MUTE:movies:24"}',
        ])
        resp = mock.Mock(text=ndjson)
        resp.raise_for_status = mock.Mock()
        state: dict = {"control": {"last_id": "id0"}}
        with _env(NTFY_CONTROL_TOPIC="ctl"), \
                mock.patch.object(control.requests, "get", return_value=resp) as get:
            cmds = control.poll(state)
        self.assertEqual(cmds, ["DONE:water", "MUTE:movies:24"])
        self.assertEqual(state["control"]["last_id"], "id3")
        # since= carries the previous cursor.
        self.assertEqual(get.call_args.kwargs["params"]["since"], "id0")

    def test_first_poll_uses_since_all(self):
        resp = mock.Mock(text="")
        resp.raise_for_status = mock.Mock()
        with _env(NTFY_CONTROL_TOPIC="ctl"), \
                mock.patch.object(control.requests, "get", return_value=resp) as get:
            self.assertEqual(control.poll({}), [])
        self.assertEqual(get.call_args.kwargs["params"]["since"], "all")

    def test_network_error_returns_empty_without_advancing_cursor(self):
        state: dict = {"control": {"last_id": "id0"}}
        with _env(NTFY_CONTROL_TOPIC="ctl"), \
                mock.patch.object(control.requests, "get",
                                  side_effect=OSError("boom")):
            self.assertEqual(control.poll(state), [])
        self.assertEqual(state["control"]["last_id"], "id0")


class DoneTest(unittest.TestCase):
    def test_inserts_next_unsent_slot_key(self):
        # 14:30 UTC: the 12 slot fired (and was marked); next nudge is 15:00.
        state = {"water_slots_sent": [habits._slot_key(NOW.date(), 12)]}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            control.cmd_done("water", state, now=NOW)
        self.assertIn("2026-06-10|15", state["water_slots_sent"])
        # Only the NEXT nudge is suppressed; later slots still fire.
        self.assertNotIn("2026-06-10|18", state["water_slots_sent"])
        self.assertNotIn("2026-06-10|21", state["water_slots_sent"])

    def test_idempotent_repeat(self):
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            control.cmd_done("water", state, now=NOW)
            before = list(state["water_slots_sent"])
            control.cmd_done("water", state, now=NOW)
            control.cmd_done("water", state, now=NOW)
        # Repeats re-suppress the same next slot, never march further ahead.
        self.assertEqual(state["water_slots_sent"], before)
        self.assertEqual(before, ["2026-06-10|15"])

    def test_already_due_slot_is_not_the_next_nudge(self):
        # 14:30 with the 12 slot unsent (delayed run): it fires this same run
        # anyway, so DONE still targets the next FUTURE slot, 15:00.
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            control.cmd_done("water", state, now=NOW)
        self.assertEqual(state["water_slots_sent"], ["2026-06-10|15"])

    def test_no_slots_left_today_is_a_noop(self):
        late = NOW.replace(hour=22)  # past the last (21:00) slot
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            control.cmd_done("water", state, now=late)
        self.assertEqual(state, {})

    def test_unknown_habit_fails_closed(self):
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            control.cmd_done("nope", state, now=NOW)
        self.assertEqual(state, {})


class SnoozeTest(unittest.TestCase):
    def test_writes_until_iso(self):
        state: dict = {}
        control.cmd_snooze("passport", 60, state, now=NOW)
        self.assertEqual(state["snoozed"]["passport"],
                         (NOW + _dt.timedelta(minutes=60)).isoformat())

    def test_minutes_are_clamped(self):
        state: dict = {}
        control.cmd_snooze("passport", 999_999, state, now=NOW)
        self.assertEqual(
            state["snoozed"]["passport"],
            (NOW + _dt.timedelta(minutes=control.MAX_SNOOZE_MINUTES)).isoformat())
        control.cmd_snooze("passport", 0, state, now=NOW)
        self.assertEqual(
            state["snoozed"]["passport"],
            (NOW + _dt.timedelta(minutes=control.MIN_SNOOZE_MINUTES)).isoformat())


class MuteTest(unittest.TestCase):
    def test_writes_until_iso(self):
        state: dict = {}
        control.cmd_mute("movies", 24, state, now=NOW)
        self.assertEqual(state["muted"]["movies"],
                         (NOW + _dt.timedelta(hours=24)).isoformat())

    def test_hours_are_clamped(self):
        state: dict = {}
        control.cmd_mute("movies", 9999, state, now=NOW)
        self.assertEqual(
            state["muted"]["movies"],
            (NOW + _dt.timedelta(hours=control.MAX_MUTE_HOURS)).isoformat())

    def test_active_mute_downgrades_digest_to_drop(self):
        until = (_dt.datetime.now(UTC) + _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"movies": until}}
        with capture_pushes() as sent:
            events.emit(
                state, title="Trailer drop", topic="movies", source="Movies",
                legacy_action="digest", score=10,
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(sent, [])
        self.assertFalse(state.get("digest_buffer"))  # dropped, not buffered

    def test_expired_mute_does_not_enforce(self):
        until = (_dt.datetime.now(UTC) - _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"movies": until}}
        with capture_pushes():
            events.emit(
                state, title="Trailer drop", topic="movies", source="Movies",
                legacy_action="digest", score=10,
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(len(state["digest_buffer"]), 1)  # buffered as normal

    def test_mute_never_touches_live_pushes(self):
        until = (_dt.datetime.now(UTC) + _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"movies": until}}
        with capture_pushes() as sent:
            events.emit(
                state, title="Big premiere", topic="movies", source="Movies",
                legacy_priority="high", legacy_action="push",
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(len(sent), 1)  # a push routed live still rings


class UntilActiveTest(unittest.TestCase):
    def test_future_is_active(self):
        self.assertTrue(control.until_active(
            (NOW + _dt.timedelta(minutes=1)).isoformat(), now=NOW))

    def test_past_missing_and_malformed_are_inactive(self):
        self.assertFalse(control.until_active(
            (NOW - _dt.timedelta(minutes=1)).isoformat(), now=NOW))
        self.assertFalse(control.until_active(None, now=NOW))
        self.assertFalse(control.until_active("not-a-date", now=NOW))


class DispatchTest(unittest.TestCase):
    def test_routes_known_commands(self):
        state: dict = {}
        with mock.patch.object(habits, "_load", return_value=[WATER]):
            control.dispatch(
                ["DONE:water", "SNOOZE:passport:60", "MUTE:movies:24"], state)
        self.assertTrue(state["water_slots_sent"])
        self.assertIn("passport", state["snoozed"])
        self.assertIn("movies", state["muted"])

    def test_unknown_and_malformed_are_dropped(self):
        state: dict = {}
        control.dispatch(
            ["RUN:rm -rf /", "DONE:", "SNOOZE:passport:abc", "MUTE:movies",
             "done:water", ""], state)
        self.assertEqual(state, {})  # nothing matched, nothing mutated

    def test_one_failing_command_never_blocks_the_rest(self):
        state: dict = {}
        with mock.patch.object(control, "cmd_done", side_effect=RuntimeError("x")):
            control.dispatch(["DONE:water", "MUTE:movies:24"], state)
        self.assertIn("movies", state["muted"])


class MakeActionTest(unittest.TestCase):
    def test_disabled_returns_none(self):
        with _env():
            self.assertIsNone(control.make_action("Done", "DONE:water"))

    def test_builds_http_action_for_control_topic(self):
        with _env(NTFY_CONTROL_TOPIC="ctl-abc"):
            action = control.make_action("Done", "DONE:water")
        self.assertEqual(action, {
            "action": "http",
            "label": "Done",
            "url": "https://ntfy.sh/ctl-abc",
            "method": "POST",
            "body": "DONE:water",
            "clear": True,
        })


class ButtonWiringTest(unittest.TestCase):
    def test_habit_push_has_no_actions_when_disabled(self):
        now = _dt.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        with _env(), capture_pushes() as sent:
            habits._run_one({}, WATER, now)
        self.assertNotIn("actions", sent[0])  # byte-identical to today

    def test_habit_push_carries_done_button_when_enabled(self):
        now = _dt.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        with _env(NTFY_CONTROL_TOPIC="ctl"), capture_pushes() as sent:
            habits._run_one({}, WATER, now)
        self.assertEqual(sent[0]["actions"][0]["body"], "DONE:water")


if __name__ == "__main__":
    unittest.main()
