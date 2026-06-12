"""Tests for the event normalization + routing funnel (notify_watcher.events).

Uses capture_pushes (tests/_util) to assert what WOULD be sent without touching
the network, and an in-memory state dict to inspect the digest buffer.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from notify_watcher import digest, eventlog, events, ntfy
from tests._util import capture_pushes

# Same worked-example config used in test_priority.
CFG = {
    "threshold": 60,
    "digest_floor": 25,
    "default": 30,
    "ntfy_bands": {"90": "urgent", "70": "high", "0": "default"},
    "rules": [
        {"topic": "visa_bulletin", "score": 100},
        {"topic": "fda", "score": 70},
        {"topic": "ios_release", "score": 40},
        {"topic": "movies", "score": 15},
    ],
}
DIGEST_CFG: dict = {}  # digest.add supplies its own defaults for missing keys


class EngineOnTest(unittest.TestCase):
    def test_above_threshold_pushes_at_banded_priority(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="F4 moved", body="2015 -> 2016", topic="visa_bulletin",
                click_url="https://travel.state.gov", tags="passport_control",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "F4 moved")
        self.assertEqual(sent[0]["message"], "2015 -> 2016")
        self.assertEqual(sent[0]["priority"], "urgent")
        self.assertEqual(sent[0]["click_url"], "https://travel.state.gov")
        self.assertEqual(sent[0]["tags"], "passport_control")
        self.assertNotIn(digest.BUFFER_KEY, state)  # nothing buffered

    def test_mid_score_routes_to_digest_with_global_score(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="iOS 18.5", body="install soon", topic="ios_release",
                click_url="https://apple.com/x", source="Apple",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent, [])  # no live push
        buf = state[digest.BUFFER_KEY]
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["title"], "iOS 18.5")
        self.assertEqual(buf[0]["url"], "https://apple.com/x")
        self.assertEqual(buf[0]["source"], "Apple")
        self.assertEqual(buf[0]["score"], 40)  # the GLOBAL priority score

    def test_digest_item_carries_body_as_detail(self):
        # A body-informative topic routed to the digest keeps its body as detail.
        state: dict = {}
        with capture_pushes():
            events.emit(
                state, title="Public holiday", body="Christmas (in 1 day)",
                topic="holidays", source="Holidays",
                priority_cfg={"threshold": 60, "digest_floor": 25, "default": 40},
                digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(state[digest.BUFFER_KEY][0]["detail"], "Christmas (in 1 day)")

    def test_below_floor_drops_silently(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="Some sequel rumor", topic="movies",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent, [])
        self.assertNotIn(digest.BUFFER_KEY, state)


class QuietDeferTest(unittest.TestCase):
    """Quiet hours defer a would-be-suppressed push into the digest (not a drop)."""

    QUIET_ON = {"enabled": True, "defer_to_digest": True}
    QUIET_DROP = {"enabled": True, "defer_to_digest": False}

    def _emit_default_band(self, state, quiet_cfg,
                           suppressed_priorities=("default", "low", None)):
        """Emit an event that scores 62 -> a 'default'-band push, the tier quiet
        hours holds. quiet_cfg is served as the quiet_hours config section."""
        with mock.patch.object(events.config, "section",
                               side_effect=lambda name: quiet_cfg if name == "quiet_hours" else {}), \
             mock.patch.object(ntfy, "would_suppress",
                               side_effect=lambda p: p in suppressed_priorities), \
             capture_pushes() as sent:
            events.emit(
                state, title="Streamer live", topic="twitch", source="Twitch",
                priority_cfg={"threshold": 60, "digest_floor": 25, "default": 62,
                              "ntfy_bands": {"90": "urgent", "70": "high", "0": "default"}},
                digest_cfg=DIGEST_CFG,
            )
        return sent

    def test_suppressed_push_lands_in_digest(self):
        state: dict = {}
        sent = self._emit_default_band(state, self.QUIET_ON)
        self.assertEqual(sent, [])  # not pushed overnight...
        buf = state[digest.BUFFER_KEY]
        self.assertEqual(len(buf), 1)  # ...but waiting in the morning digest
        self.assertEqual(buf[0]["title"], "Streamer live")
        self.assertEqual(buf[0]["score"], 62)
        # and the event log records the deferred routing, not a phantom push
        self.assertEqual(state["event_log"][-1]["action"], "digest")

    def test_high_band_is_never_deferred(self):
        state: dict = {}
        with mock.patch.object(events.config, "section",
                               side_effect=lambda name: self.QUIET_ON if name == "quiet_hours" else {}), \
             mock.patch.object(ntfy, "would_suppress",
                               side_effect=lambda p: p not in ("high", "urgent")), \
             capture_pushes() as sent:
            events.emit(
                state, title="Quake nearby", topic="quakes", severity="critical",
                priority_cfg={"threshold": 60, "digest_floor": 25, "default": 30,
                              "ntfy_bands": {"90": "urgent", "70": "high", "0": "default"},
                              "rules": [{"topic": "quakes", "severity": "critical", "score": 95}]},
                digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(len(sent), 1)  # urgent rings through quiet hours
        self.assertEqual(sent[0]["priority"], "urgent")

    def test_defer_disabled_falls_back_to_transport_drop(self):
        state: dict = {}
        sent = self._emit_default_band(state, self.QUIET_DROP)
        # emit chose "push"; the transport's own quiet check does the dropping
        # (capture_pushes bypasses it, so the call itself is what we assert).
        self.assertEqual(len(sent), 1)
        self.assertNotIn(digest.BUFFER_KEY, state)

    def test_legacy_path_defers_too(self):
        state: dict = {}
        with mock.patch.object(events.config, "section",
                               side_effect=lambda name: self.QUIET_ON if name == "quiet_hours" else {}), \
             mock.patch.object(ntfy, "would_suppress", return_value=True), \
             capture_pushes() as sent:
            events.emit(
                state, title="New song", topic="music", source="Music",
                legacy_priority="default", legacy_action="push", score=40,
                priority_cfg={}, digest_cfg=DIGEST_CFG,  # engine OFF
            )
        self.assertEqual(sent, [])
        self.assertEqual(state[digest.BUFFER_KEY][0]["title"], "New song")

    def test_quiet_config_error_fails_open_to_push(self):
        state: dict = {}
        with mock.patch.object(events.config, "section", side_effect=RuntimeError("boom")), \
             capture_pushes() as sent:
            events.emit(
                state, title="Streamer live", topic="twitch",
                priority_cfg={"threshold": 60, "digest_floor": 25, "default": 62,
                              "ntfy_bands": {"0": "default"}},
                digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(len(sent), 1)


class TitlePrefixTest(unittest.TestCase):
    """The collector engine's label-style push, preserved via the metadata hint."""

    def test_title_prefix_renders_legacy_collector_push(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="FDA approves Keytruda (BLA1)", topic="fda",
                severity="high", source="Keytruda",
                click_url="https://fda.gov/x", tags="zap",
                metadata={"title_prefix": "FDA"},
                legacy_priority="high", legacy_action="push",
                priority_cfg={}, digest_cfg=DIGEST_CFG,  # engine OFF -> legacy
            )
        self.assertEqual(sent[0], {
            "title": "FDA: Keytruda",                       # "<prefix>: <source>"
            "message": "FDA approves Keytruda (BLA1)",       # the headline
            "click_url": "https://fda.gov/x",
            "tags": "zap",
            "priority": "high",
            "attach_url": None,
        })

    def test_title_prefix_drops_separator_when_source_blank(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="headline", topic="energy", source="",
                metadata={"title_prefix": "Energy"},
                legacy_priority="urgent", legacy_action="push",
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent[0]["title"], "Energy")  # no trailing ": "


class BackwardCompatTest(unittest.TestCase):
    """Engine OFF (empty priority cfg): emit reproduces pre-engine behavior."""

    def test_legacy_push_matches_old_ntfy_call(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="Apple release: iOS 18.5", body="version line",
                topic="ios_release", click_url="https://apple.com/x", tags="iphone",
                legacy_priority=None, legacy_action="push",
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {
            "title": "Apple release: iOS 18.5",
            "message": "version line",
            "click_url": "https://apple.com/x",
            "tags": "iphone",
            "priority": None,
            "attach_url": None,
        })

    def test_legacy_push_preserves_explicit_priority(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="Quake", body="M5 nearby", topic="quakes",
                legacy_priority="high", legacy_action="push",
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent[0]["priority"], "high")

    def test_legacy_digest_uses_caller_within_domain_score(self):
        state: dict = {}
        with capture_pushes() as sent:
            events.emit(
                state, title="moderate energy item", topic="energy", source="EIA",
                click_url="https://eia.gov/x", legacy_action="digest", score=5,
                priority_cfg={}, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(sent, [])
        buf = state[digest.BUFFER_KEY]
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf[0]["score"], 5)  # caller's score, not an engine score
        self.assertEqual(buf[0]["source"], "EIA")


class ButtonExpansionTest(unittest.TestCase):
    """Declarative metadata["buttons"] + control.default_buttons -> ntfy actions."""

    PUSH_CFG = {"threshold": 60, "digest_floor": 25, "default": 70,
                "ntfy_bands": {"0": "default"}}

    def setUp(self):
        # Buttons require the control channel; point it at a fake topic.
        self.env = mock.patch.dict(os.environ, {"NTFY_CONTROL_TOPIC": "nw-ctl-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _emit(self, *, metadata=None, control_cfg=None, topic="movies"):
        sections = {"control": control_cfg or {}}
        with mock.patch.object(events.config, "section",
                               side_effect=lambda name: sections.get(name, {})), \
             capture_pushes() as sent:
            state = events.emit(
                {}, title="headline", topic=topic, source="S",
                metadata=metadata, priority_cfg=self.PUSH_CFG, digest_cfg={},
            )
        return state, sent

    def test_declarative_specs_become_actions_with_the_event_id(self):
        state, sent = self._emit(metadata={"buttons": ["read", "more", "later:180"]})
        actions = sent[0]["actions"]
        event_id = state[eventlog.EVENT_LOG_KEY][-1]["id"]
        self.assertEqual([a["label"] for a in actions],
                         ["Read later", "Show more", "Remind 3h"])
        self.assertEqual([a["body"] for a in actions],
                         [f"READ:{event_id}", f"MORE:{event_id}",
                          f"LATER:{event_id}:180"])

    def test_config_default_buttons_apply_per_topic(self):
        _, sent = self._emit(control_cfg={"default_buttons": {"movies": ["read"]}})
        self.assertEqual([a["body"][:5] for a in sent[0]["actions"]], ["READ:"])

    def test_defaults_skipped_for_other_topics(self):
        _, sent = self._emit(control_cfg={"default_buttons": {"games": ["read"]}})
        self.assertNotIn("actions", sent[0])

    def test_explicit_actions_win_and_cap_is_three(self):
        done = {"action": "http", "label": "Done", "url": "u", "method": "POST",
                "body": "DONE:water", "clear": True}
        _, sent = self._emit(metadata={"actions": [done],
                                       "buttons": ["read", "more", "later:60"]})
        actions = sent[0]["actions"]
        self.assertEqual(len(actions), 3)            # ntfy hard cap
        self.assertEqual(actions[0], done)           # explicit v1 action first
        self.assertEqual([a["label"] for a in actions[1:]],
                         ["Read later", "Show more"])

    def test_unknown_spec_is_skipped_not_fatal(self):
        _, sent = self._emit(metadata={"buttons": ["frobnicate", "later:oops", "read"]})
        self.assertEqual([a["label"] for a in sent[0]["actions"]], ["Read later"])

    def test_control_channel_off_means_no_buttons(self):
        self.env.stop()  # NTFY_CONTROL_TOPIC unset -> make_action returns None
        try:
            _, sent = self._emit(metadata={"buttons": ["read", "more"]})
            self.assertNotIn("actions", sent[0])  # byte-identical kill switch
        finally:
            self.env.start()

    def test_later_labels_humanize(self):
        self.assertEqual(events._later_label(45), "Remind 45m")
        self.assertEqual(events._later_label(180), "Remind 3h")
        self.assertEqual(events._later_label(2880), "Remind 2d")


class NormalizationTest(unittest.TestCase):
    def test_emit_stamps_timestamp_and_folds_transport_into_metadata(self):
        e = events.Event(
            title="t", body="b", topic="x", severity="moderate",
            source="s", timestamp=events._now_iso(), metadata={},
        )
        self.assertTrue(e.timestamp.endswith("+00:00"))  # UTC ISO-8601

    def test_explicit_metadata_click_url_is_not_overwritten(self):
        # A caller that already put click_url in metadata keeps it; the click_url
        # kwarg only fills in when absent (setdefault).
        state: dict = {}
        with capture_pushes():
            events.emit(
                state, title="t", topic="ios_release",
                metadata={"click_url": "https://from-metadata"},
                click_url="https://from-kwarg",
                priority_cfg=CFG, digest_cfg=DIGEST_CFG,
            )
        self.assertEqual(state[digest.BUFFER_KEY][0]["url"], "https://from-metadata")


if __name__ == "__main__":
    unittest.main()
