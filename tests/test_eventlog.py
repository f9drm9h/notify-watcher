"""Tests for the append-only event-log sink (notify_watcher.eventlog).

The log is the dashboard's data source: every routed Event is recorded with its
routing action and global score, capped to a ring so state.json can't grow without
bound. These pin the record shape, the cap/eviction (oldest-first), and that emit
records on all three routes (push/digest/drop) plus the legacy path — so the
dashboard sees a complete history regardless of whether the engine is on.
"""
from __future__ import annotations

import unittest

from notify_watcher import eventlog, events
from tests._util import capture_pushes

# Engine config used by the emit-integration cases: rule bands a known score so we
# can assert the recorded action. Mirrors the shape priority.decide expects.
ENGINE = {
    "threshold": 60,
    "digest_floor": 25,
    "default": 0,
    "rules": [
        {"topic": "hi", "score": 90},
        {"topic": "mid", "score": 40},
        {"topic": "lo", "score": 5},
    ],
}


def _event(topic="games", title="t", body="b", source="RAWG", click=""):
    return events.Event(
        title=title, body=body, topic=topic, severity="high",
        source=source, timestamp="2026-06-08T14:00:00+00:00",
        metadata={"click_url": click} if click else {},
    )


class RecordShapeTest(unittest.TestCase):
    def test_record_captures_normalized_event_plus_decision(self):
        state: dict = {}
        eventlog.record(state, _event(click="https://x/1"), "push", 95)
        self.assertEqual(state[eventlog.EVENT_LOG_KEY], [{
            "ts": "2026-06-08T14:00:00+00:00",
            "topic": "games",
            "title": "t",
            "source": "RAWG",
            "severity": "high",
            "score": 95,
            "action": "push",
            "detail": "b",          # the Event body -> change-summary line
            "url": "https://x/1",
        }])

    def test_url_defaults_to_empty_when_no_click(self):
        state: dict = {}
        eventlog.record(state, _event(), "digest", 40)
        self.assertEqual(state[eventlog.EVENT_LOG_KEY][0]["url"], "")

    def test_appends_in_order(self):
        state: dict = {}
        eventlog.record(state, _event(title="a"), "push", 90)
        eventlog.record(state, _event(title="b"), "drop", 1)
        titles = [e["title"] for e in state[eventlog.EVENT_LOG_KEY]]
        self.assertEqual(titles, ["a", "b"])


class CapTest(unittest.TestCase):
    def test_oldest_dropped_when_over_cap(self):
        state: dict = {}
        cfg = {"event_log_max": 3}
        for i in range(5):
            eventlog.record(state, _event(title=str(i)), "push", 90, cfg)
        titles = [e["title"] for e in state[eventlog.EVENT_LOG_KEY]]
        self.assertEqual(titles, ["2", "3", "4"])  # 0,1 evicted oldest-first

    def test_lowered_cap_trims_multiple_in_one_call(self):
        state = {eventlog.EVENT_LOG_KEY: [{"title": str(i)} for i in range(10)]}
        eventlog.record(state, _event(title="new"), "push", 90, {"event_log_max": 3})
        titles = [e["title"] for e in state[eventlog.EVENT_LOG_KEY]]
        self.assertEqual(titles, ["8", "9", "new"])

    def test_bad_cap_falls_back_to_default(self):
        self.assertEqual(eventlog._cap({"event_log_max": "oops"}), eventlog._DEFAULT_MAX)
        self.assertEqual(eventlog._cap({"event_log_max": 0}), eventlog._DEFAULT_MAX)
        self.assertEqual(eventlog._cap(None), eventlog._DEFAULT_MAX)


class EmitIntegrationTest(unittest.TestCase):
    """emit must record on every route, engine ON and OFF."""

    def test_engine_push_recorded_with_global_score(self):
        with capture_pushes():
            state = events.emit({}, title="x", topic="hi", source="S",
                                priority_cfg=ENGINE)
        log = state[eventlog.EVENT_LOG_KEY]
        self.assertEqual((log[0]["action"], log[0]["score"]), ("push", 90))

    def test_engine_digest_recorded(self):
        with capture_pushes():
            state = events.emit({}, title="x", topic="mid", source="S",
                                priority_cfg=ENGINE)
        log = state[eventlog.EVENT_LOG_KEY]
        self.assertEqual((log[0]["action"], log[0]["score"]), ("digest", 40))

    def test_engine_drop_still_recorded(self):
        with capture_pushes() as sent:
            state = events.emit({}, title="x", topic="lo", source="S",
                                priority_cfg=ENGINE)
        self.assertEqual(sent, [])  # nothing sent
        log = state[eventlog.EVENT_LOG_KEY]
        self.assertEqual((log[0]["action"], log[0]["score"]), ("drop", 5))  # but logged

    def test_legacy_path_recorded_with_within_domain_score(self):
        # No priority section -> engine OFF -> legacy routing, still logged.
        with capture_pushes():
            state = events.emit({}, title="x", topic="games", source="S",
                                legacy_action="digest", score=7, priority_cfg={})
        log = state[eventlog.EVENT_LOG_KEY]
        self.assertEqual((log[0]["action"], log[0]["score"]), ("digest", 7))


if __name__ == "__main__":
    unittest.main()
