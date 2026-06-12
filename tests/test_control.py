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

    def test_active_mute_defers_live_push_to_digest(self):
        # THE fix for the field report "tapped MUTE, pushes kept firing":
        # a muted topic's live push must stop ringing and land in the digest
        # buffer instead (defer, don't drop).
        until = (_dt.datetime.now(UTC) + _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"movies": until}}
        with capture_pushes() as sent:
            events.emit(
                state, title="Trailer leak storm", topic="movies", source="Movies",
                legacy_priority="high", legacy_action="push", score=10,
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(sent, [])  # no ring
        buf = state.get("digest_buffer") or []
        self.assertEqual(len(buf), 1)  # deferred, not lost
        self.assertEqual(buf[0]["title"], "Trailer leak storm")

    def test_critical_severity_breaks_through_mute(self):
        # Muting chatty news must never silence a real alert.
        until = (_dt.datetime.now(UTC) + _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"weather": until}}
        with capture_pushes() as sent:
            events.emit(
                state, title="Hurricane warning", topic="weather",
                severity="critical", source="INDOMET",
                legacy_priority="urgent", legacy_action="push",
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(len(sent), 1)

    def test_mute_defers_engine_routed_push_too(self):
        # Same enforcement on the priority-engine path (the production mode):
        # a rule that scores movies above the push threshold still defers
        # while the mute is active.
        until = (_dt.datetime.now(UTC) + _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"movies": until}}
        engine_cfg = {"rules": [{"topic": "movies", "score": 80}]}
        with capture_pushes() as sent:
            events.emit(
                state, title="Trailer leak storm", topic="movies", source="Movies",
                severity="high", legacy_action="push",
                priority_cfg=engine_cfg, digest_cfg={},
            )
        self.assertEqual(sent, [])
        self.assertEqual(len(state.get("digest_buffer") or []), 1)

    def test_mute_does_not_touch_other_topics(self):
        until = (_dt.datetime.now(UTC) + _dt.timedelta(hours=1)).isoformat()
        state = {"muted": {"movies": until}}
        with capture_pushes() as sent:
            events.emit(
                state, title="New game date", topic="games", source="Games",
                legacy_priority="high", legacy_action="push",
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(len(sent), 1)


class MuteEndToEndTest(unittest.TestCase):
    """The full reply-button mute flow: button POST -> poll -> dispatch ->
    enforcement at emit -> expiry, exactly as a real run executes it."""

    def _poll_and_dispatch(self, state):
        ndjson = "\n".join([
            '{"id": "m1", "event": "message", "message": "MUTE:movies:24"}',
            '{"id": "m2", "event": "message", "message": "MUTE:games:24"}',
        ])
        resp = mock.Mock(text=ndjson)
        resp.raise_for_status = mock.Mock()
        with _env(NTFY_CONTROL_TOPIC="ctl"), \
                mock.patch.object(control.requests, "get", return_value=resp):
            control.dispatch(control.poll(state), state)

    def test_button_tap_silences_both_topics_same_run(self):
        state: dict = {}
        self._poll_and_dispatch(state)
        self.assertEqual(state["control"]["last_id"], "m2")
        with capture_pushes() as sent:
            events.emit(
                state, title="Movie push", topic="movies", source="M",
                legacy_priority="high", legacy_action="push", score=10,
                priority_cfg={}, digest_cfg={},
            )
            events.emit(
                state, title="Game digest item", topic="games", source="G",
                legacy_action="digest", score=10,
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(sent, [])  # nothing rang
        buf = state.get("digest_buffer") or []
        self.assertEqual([i["title"] for i in buf], ["Movie push"])  # deferred
        # The game digest item was dropped outright (the chatter the mute
        # was aimed at), not buffered.

    def test_mute_expires_and_pushes_resume(self):
        state: dict = {}
        self._poll_and_dispatch(state)
        # Rewind both mutes into the past, as if 24h elapsed.
        past = (_dt.datetime.now(UTC) - _dt.timedelta(minutes=1)).isoformat()
        state["muted"] = {t: past for t in state["muted"]}
        with capture_pushes() as sent:
            events.emit(
                state, title="Movie push", topic="movies", source="M",
                legacy_priority="high", legacy_action="push",
                priority_cfg={}, digest_cfg={},
            )
        self.assertEqual(len(sent), 1)  # rings again after expiry


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
        # Pin the clock: dispatch routes DONE through the real current time,
        # and past the day's last water slot it is (correctly) a no-op — the
        # test must not depend on the hour it happens to run at.
        with mock.patch.object(habits, "_load", return_value=[WATER]), \
                mock.patch.object(control, "_utcnow", return_value=NOW):
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


EID = "ab12cd34ef56ab78"   # a valid 16-hex event-log id
EID2 = "ab12cd34ef56ab79"
LOG_ENTRY = {
    "id": EID, "ts": NOW.isoformat(), "topic": "movies", "title": "Trailer",
    "source": "Movies", "severity": "high", "score": 70, "action": "push",
    "detail": "Full detail line", "url": "https://x/article",
}


def _log_state(extra_entries=()):
    return {"event_log": [LOG_ENTRY, *extra_entries]}


class ReadTest(unittest.TestCase):
    def test_saves_log_fields_only(self):
        state = _log_state()
        control.cmd_read(EID, state, now=NOW)
        self.assertEqual(state["reading_list"], [{
            "id": EID, "title": "Trailer", "url": "https://x/article",
            "source": "Movies", "added": NOW.isoformat(),
        }])

    def test_idempotent_repeat(self):
        state = _log_state()
        control.cmd_read(EID, state, now=NOW)
        control.cmd_read(EID, state, now=NOW)
        self.assertEqual(len(state["reading_list"]), 1)

    def test_unknown_event_fails_closed(self):
        state = _log_state()
        control.cmd_read("0" * 16, state, now=NOW)
        self.assertNotIn("reading_list", state)

    def test_fifo_cap(self):
        state = _log_state()
        state["reading_list"] = [
            {"id": f"{i:016x}", "title": str(i)}
            for i in range(control.MAX_READING_LIST)
        ]
        control.cmd_read(EID, state, now=NOW)
        self.assertEqual(len(state["reading_list"]), control.MAX_READING_LIST)
        # Oldest dropped, newest (the new save) kept.
        self.assertEqual(state["reading_list"][-1]["id"], EID)
        self.assertNotEqual(state["reading_list"][0]["title"], "0")


class MoreTest(unittest.TestCase):
    def test_queues_request(self):
        state = _log_state()
        control.cmd_more(EID, state)
        self.assertEqual(state["more_requests"], {EID: True})

    def test_unknown_event_fails_closed(self):
        state = _log_state()
        control.cmd_more("0" * 16, state)
        self.assertNotIn("more_requests", state)


class LaterTest(unittest.TestCase):
    def test_snapshots_the_event(self):
        state = _log_state()
        control.cmd_later(EID, 180, state, now=NOW)
        entry = state["later"][EID]
        self.assertEqual(entry["until"],
                         (NOW + _dt.timedelta(minutes=180)).isoformat())
        self.assertEqual(entry["snapshot"], {
            "title": "Trailer", "detail": "Full detail line",
            "url": "https://x/article", "source": "Movies", "topic": "movies",
        })

    def test_minutes_are_clamped(self):
        state = _log_state()
        control.cmd_later(EID, 999_999, state, now=NOW)
        self.assertEqual(
            state["later"][EID]["until"],
            (NOW + _dt.timedelta(minutes=control.MAX_LATER_MINUTES)).isoformat())

    def test_repeat_overwrites_not_duplicates(self):
        state = _log_state()
        control.cmd_later(EID, 60, state, now=NOW)
        control.cmd_later(EID, 180, state, now=NOW)
        self.assertEqual(len(state["later"]), 1)
        self.assertEqual(state["later"][EID]["until"],
                         (NOW + _dt.timedelta(minutes=180)).isoformat())

    def test_queue_cap_drops_new_ids(self):
        state = _log_state()
        state["later"] = {f"{i:016x}": {"until": "x", "snapshot": {}}
                          for i in range(control.MAX_LATER)}
        control.cmd_later(EID, 60, state, now=NOW)
        self.assertNotIn(EID, state["later"])

    def test_unknown_event_fails_closed(self):
        state = _log_state()
        control.cmd_later("0" * 16, 60, state, now=NOW)
        self.assertNotIn("later", state)


class UnmuteTest(unittest.TestCase):
    def test_removes_active_mute(self):
        state = {"muted": {"movies": "2099-01-01T00:00:00+00:00"}}
        control.cmd_unmute("movies", state)
        self.assertEqual(state["muted"], {})

    def test_unmuted_topic_is_a_noop(self):
        state: dict = {}
        control.cmd_unmute("movies", state)
        self.assertEqual(state, {})


class DispatchV2Test(unittest.TestCase):
    def test_routes_item_level_verbs(self):
        state = _log_state()
        with mock.patch.object(control, "_utcnow", return_value=NOW):
            control.dispatch(
                [f"READ:{EID}", f"MORE:{EID}", f"LATER:{EID}:60",
                 "MUTE:games:24", "UNMUTE:games"], state)
        self.assertEqual(len(state["reading_list"]), 1)
        self.assertIn(EID, state["more_requests"])
        self.assertIn(EID, state["later"])
        self.assertEqual(state["muted"], {})  # muted then unmuted

    def test_malformed_ids_are_dropped(self):
        state = _log_state()
        control.dispatch(
            ["READ:short", f"READ:{EID.upper()}", "READ:zz12cd34ef56ab78",
             f"LATER:{EID}:abc", "MORE:", "UNMUTE:"], state)
        self.assertNotIn("reading_list", state)
        self.assertNotIn("more_requests", state)
        self.assertNotIn("later", state)


PRODUCT = {"name": "Anker Prime 250W", "url": "https://anker.com/a1340"}


class OfferRegistryTest(unittest.TestCase):
    def test_register_returns_deterministic_id_and_records_offer(self):
        s1, s2 = {}, {}
        oid = control.register_offer(s1, "product", "Anker Prime 250W",
                                     PRODUCT, now=NOW)
        self.assertEqual(oid, control.register_offer(
            s2, "product", "Anker Prime 250W", PRODUCT, now=NOW))
        self.assertEqual(s1["offers"][oid], {
            "kind": "product", "label": "Anker Prime 250W",
            "payload": PRODUCT, "created": NOW.isoformat(), "applied": None,
        })

    def test_reregister_keeps_created_and_applied(self):
        state: dict = {}
        oid = control.register_offer(state, "product", "Old name", PRODUCT,
                                     applied=True, now=NOW)
        later = NOW + _dt.timedelta(days=3)
        control.register_offer(state, "product", "New name", PRODUCT, now=later)
        offer = state["offers"][oid]
        self.assertEqual(offer["label"], "New name")          # refreshed
        self.assertEqual(offer["created"], NOW.isoformat())   # kept
        self.assertEqual(offer["applied"], NOW.isoformat())   # kept

    def test_ignored_offer_returns_none(self):
        state: dict = {}
        oid = control.register_offer(state, "product", "P", PRODUCT, now=NOW)
        control.cmd_ignore(oid, state, now=NOW)
        self.assertIsNone(control.register_offer(state, "product", "P",
                                                 PRODUCT, now=NOW))


class AddUndoIgnoreTest(unittest.TestCase):
    def _offered(self, applied=False):
        state: dict = {}
        oid = control.register_offer(state, "product", "P", PRODUCT,
                                     applied=applied, now=NOW)
        if applied:
            # the auto-track case: the topic itself put it in auto_products
            state["auto_products"] = [dict(PRODUCT)]
        return state, oid

    def test_add_applies_to_tracked_products(self):
        state, oid = self._offered()
        control.cmd_add(oid, state, now=NOW)
        self.assertEqual(state["tracked_products"], [PRODUCT])
        self.assertEqual(state["offers"][oid]["applied"], NOW.isoformat())

    def test_add_is_idempotent(self):
        state, oid = self._offered()
        control.cmd_add(oid, state, now=NOW)
        control.cmd_add(oid, state, now=NOW)
        self.assertEqual(len(state["tracked_products"]), 1)

    def test_add_unknown_offer_fails_closed(self):
        state: dict = {}
        control.cmd_add("0" * 16, state, now=NOW)
        self.assertNotIn("tracked_products", state)

    def test_add_respects_cap(self):
        state, oid = self._offered()
        state["tracked_products"] = [
            {"name": str(i), "url": f"https://x/{i}"}
            for i in range(control.MAX_TRACKED_PRODUCTS)
        ]
        control.cmd_add(oid, state, now=NOW)
        self.assertEqual(len(state["tracked_products"]),
                         control.MAX_TRACKED_PRODUCTS)
        self.assertIsNone(state["offers"][oid]["applied"])

    def test_ignore_unapplies_and_marks(self):
        state, oid = self._offered(applied=True)
        control.cmd_ignore(oid, state, now=NOW)
        self.assertEqual(state["auto_products"], [])      # un-tracked
        self.assertIsNone(state["offers"][oid]["applied"])
        self.assertEqual(state["ignored"][oid]["was_applied"], True)

    def test_undo_of_ignore_restores_auto_applied_offer(self):
        state, oid = self._offered(applied=True)
        control.cmd_ignore(oid, state, now=NOW)
        control.cmd_undo(oid, state, now=NOW)
        self.assertNotIn(oid, state["ignored"])
        # re-applied into the ADD overlay (tracked_products)
        self.assertEqual(state["tracked_products"], [PRODUCT])
        self.assertEqual(state["offers"][oid]["applied"], NOW.isoformat())

    def test_undo_of_plain_ignore_does_not_apply(self):
        state, oid = self._offered(applied=False)
        control.cmd_ignore(oid, state, now=NOW)
        control.cmd_undo(oid, state, now=NOW)
        self.assertNotIn(oid, state["ignored"])
        self.assertEqual(state.get("tracked_products") or [], [])

    def test_undo_of_add_removes_payload(self):
        state, oid = self._offered()
        control.cmd_add(oid, state, now=NOW)
        control.cmd_undo(oid, state, now=NOW)
        self.assertEqual(state["tracked_products"], [])
        self.assertIsNone(state["offers"][oid]["applied"])

    def test_dispatch_routes_offer_verbs(self):
        state, oid = self._offered()
        with mock.patch.object(control, "_utcnow", return_value=NOW):
            control.dispatch([f"ADD:{oid}", f"IGNORE:{oid}", f"UNDO:{oid}"],
                             state)
        # add -> ignore (unapply) -> undo (restore): net applied again
        self.assertEqual(state["tracked_products"], [PRODUCT])
        self.assertNotIn(oid, state["ignored"])


class PruneOffersTest(unittest.TestCase):
    def test_expired_unapplied_pruned_applied_kept(self):
        state: dict = {}
        old = NOW - _dt.timedelta(days=control.OFFER_TTL_DAYS + 1)
        stale = control.register_offer(state, "product", "stale",
                                       {"url": "https://x/1"}, now=old)
        kept = control.register_offer(state, "product", "kept-applied",
                                      {"url": "https://x/2"}, applied=True,
                                      now=old)
        fresh = control.register_offer(state, "product", "fresh",
                                       {"url": "https://x/3"}, now=NOW)
        control._prune_offers(state, NOW)
        self.assertNotIn(stale, state["offers"])
        self.assertIn(kept, state["offers"])
        self.assertIn(fresh, state["offers"])

    def test_cap_evicts_oldest_unapplied_first(self):
        state: dict = {}
        for i in range(control.MAX_OFFERS + 2):
            control.register_offer(
                state, "product", f"p{i}", {"url": f"https://x/{i}"},
                applied=(i < 2),  # the two oldest are applied
                now=NOW + _dt.timedelta(minutes=i))
        control._prune_offers(state, NOW)
        offers = state["offers"]
        self.assertEqual(len(offers), control.MAX_OFFERS)
        # the applied ones survived even though they are oldest
        labels = {o["label"] for o in offers.values()}
        self.assertIn("p0", labels)
        self.assertIn("p1", labels)
        self.assertNotIn("p2", labels)  # oldest unapplied went first


class ProcessPendingTest(unittest.TestCase):
    def _due_later_state(self):
        state = _log_state()
        control.cmd_later(EID, 60, state, now=NOW - _dt.timedelta(hours=2))
        return state

    def test_due_later_refires_with_buttons_and_clears(self):
        state = self._due_later_state()
        with _env(NTFY_CONTROL_TOPIC="ctl"), capture_pushes() as sent:
            control.process_pending(state, now=NOW)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Reminder: Trailer")
        self.assertEqual(sent[0]["message"], "Full detail line")
        self.assertEqual(sent[0]["click_url"], "https://x/article")
        self.assertEqual([a["body"] for a in sent[0]["actions"]],
                         [f"LATER:{EID}:180", f"READ:{EID}"])
        self.assertEqual(state["later"], {})

    def test_not_due_later_stays_queued(self):
        state = _log_state()
        control.cmd_later(EID, 600, state, now=NOW)
        with _env(NTFY_CONTROL_TOPIC="ctl"), capture_pushes() as sent:
            control.process_pending(state, now=NOW)
        self.assertEqual(sent, [])
        self.assertIn(EID, state["later"])

    def test_failed_refire_is_kept_for_retry(self):
        state = self._due_later_state()
        with _env(NTFY_CONTROL_TOPIC="ctl"), \
                mock.patch.object(control.ntfy, "push", side_effect=OSError("boom")):
            control.process_pending(state, now=NOW)
        self.assertIn(EID, state["later"])

    def test_more_push_includes_detail_and_related(self):
        related = dict(LOG_ENTRY, id=EID2, title="Casting news",
                       ts=(NOW - _dt.timedelta(days=2)).isoformat())
        old = dict(LOG_ENTRY, id="ab12cd34ef56ab7a", title="Ancient news",
                   ts=(NOW - _dt.timedelta(days=30)).isoformat())
        state = _log_state([related, old])
        control.cmd_more(EID, state)
        with _env(NTFY_CONTROL_TOPIC="ctl"), capture_pushes() as sent:
            control.process_pending(state, now=NOW)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "More: Trailer")
        self.assertIn("Full detail line", sent[0]["message"])
        self.assertIn("- Casting news", sent[0]["message"])
        self.assertNotIn("Ancient news", sent[0]["message"])  # outside 7d window
        self.assertEqual(state["more_requests"], {})

    def test_more_for_aged_out_event_is_dropped(self):
        state = _log_state()
        state["more_requests"] = {"0" * 16: True}
        with _env(NTFY_CONTROL_TOPIC="ctl"), capture_pushes() as sent:
            control.process_pending(state, now=NOW)
        self.assertEqual(sent, [])
        self.assertEqual(state["more_requests"], {})

    def test_read_resolves_pending_later_snapshot_after_log_ages_out(self):
        # The Remind-again flow: log entry gone, but the LATER snapshot
        # still resolves the id (so buttons on a re-fired push keep working).
        state = _log_state()
        control.cmd_later(EID, 600, state, now=NOW)
        state["event_log"] = []          # ring aged out
        control.cmd_read(EID, state, now=NOW)
        self.assertEqual(state["reading_list"][0]["title"], "Trailer")


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
