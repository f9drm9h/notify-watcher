"""Tests for the Gemini-narrated Library of Congress story topic."""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher import config, events, priority
from notify_watcher.topics import library_of_congress as loc
from tests._util import capture_pushes

STORY = (
    "The object survived because people kept deciding it mattered. "
    "Its makers lived inside a world in motion. "
    "The record now lets a later generation step into that moment. "
    "What followed changed how the event was remembered."
)


def _raw_item(
    item_id: str = "https://www.loc.gov/item/123/",
    *,
    title: str = "March on Washington photograph",
    date: str = "1963",
    image_url: str | None = "https://www.loc.gov/static/images/sample.jpg",
) -> dict:
    raw = {
        "id": item_id,
        "url": item_id,
        "title": title,
        "date": date,
        "description": ["A public-domain Library of Congress item."],
    }
    if image_url:
        raw["image_url"] = [image_url]
    return raw


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _event(**kw):
    base = {
        "title": "LOC item",
        "body": "story",
        "topic": loc.TOPIC,
        "severity": "low",
        "source": "Library of Congress",
        "timestamp": "2026-06-12T00:00:00+00:00",
        "metadata": {},
    }
    base.update(kw)
    return events.Event(**base)


class NormalizeItemTest(unittest.TestCase):
    def test_extracts_stable_fields_and_image(self):
        item = loc._normalize_item(_raw_item(), "Civil rights movement")
        self.assertEqual(item["id"], "https://www.loc.gov/item/123/")
        self.assertEqual(item["title"], "March on Washington photograph")
        self.assertEqual(item["date"], "1963")
        self.assertEqual(item["url"], "https://www.loc.gov/item/123/")
        self.assertEqual(item["image_url"], "https://www.loc.gov/static/images/sample.jpg")
        self.assertEqual(item["focus"], "Civil rights movement")

    def test_missing_image_still_normalizes(self):
        item = loc._normalize_item(_raw_item(image_url=None), "American history milestones")
        self.assertIsNone(item["image_url"])

    def test_requires_title_and_identity(self):
        self.assertIsNone(loc._normalize_item({"id": "x"}, "focus"))
        self.assertIsNone(loc._normalize_item({"title": "Untitled"}, "focus"))


class FetchItemsTest(unittest.TestCase):
    def test_fetches_public_loc_json_api(self):
        payload = {"results": [_raw_item(), {"title": ""}]}
        with mock.patch.object(loc.requests, "get", return_value=_Response(payload)) as get:
            items = loc._fetch_items("Civil rights movement")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], loc.API_URL)
        self.assertEqual(get.call_args.kwargs["params"]["fo"], "json")
        self.assertEqual(get.call_args.kwargs["params"]["q"], "Civil rights movement")
        self.assertEqual(len(items), 1)

    def test_malformed_results_yield_empty_list(self):
        with mock.patch.object(loc.requests, "get", return_value=_Response({"results": {}})):
            self.assertEqual(loc._fetch_items("Civil rights movement"), [])


class SelectionTest(unittest.TestCase):
    def test_focus_rotates_after_last_successful_focus(self):
        state = {loc.LOC_FOCUS_KEY: loc.FOCUS_AREAS[0]}
        self.assertEqual(loc._next_focus(state), loc.FOCUS_AREAS[1])

    def test_pick_prefers_unseen_item_with_image(self):
        now = _dt.datetime(2026, 6, 12, 12)
        state = {
            loc.LOC_SEEN_KEY: {
                "seen-image": now.date().isoformat(),
            }
        }
        items = [
            {"id": "seen-image", "image_url": "https://img/seen.jpg"},
            {"id": "fresh-no-image", "image_url": None},
            {"id": "fresh-image", "image_url": "https://img/fresh.jpg"},
        ]
        picked = loc._pick_item(state, "focus", items, now)
        self.assertEqual(picked["id"], "fresh-image")

    def test_expired_seen_entries_are_pruned_on_commit(self):
        today = _dt.date(2026, 6, 12)
        stale = today - _dt.timedelta(days=loc.LOC_REPEAT_DAYS)
        state = {
            loc.LOC_SEEN_KEY: {
                "old": stale.isoformat(),
                "bad": "not-a-date",
                "fresh": (today - _dt.timedelta(days=2)).isoformat(),
            }
        }
        loc._commit(state, {"id": "new"}, loc.FOCUS_AREAS[0], today)
        self.assertEqual(
            state[loc.LOC_SEEN_KEY],
            {"fresh": "2026-06-10", "new": "2026-06-12"},
        )


class StoryGenerationTest(unittest.TestCase):
    def test_uses_required_prompt_shape(self):
        item = loc._normalize_item(_raw_item(title="A battlefield map", date="1863"),
                                   "Historic maps and exploration")
        with mock.patch.object(loc.summarize, "brief", return_value=STORY) as brief:
            loc._generate_story(item)
        brief.assert_called_once()
        prompt = brief.call_args.args[1]
        self.assertIn("Library of Congress titled 'A battlefield map'", prompt)
        self.assertIn("approximately 1863", prompt)
        self.assertIn("at least 4 substantial paragraphs", prompt)
        self.assertIn("Do not use bullet points", prompt)

    def test_none_when_gemini_returns_none(self):
        with mock.patch.object(loc.summarize, "brief", return_value=None):
            self.assertIsNone(loc._generate_story({"title": "Item", "date": "1900"}))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.now = _dt.datetime(2026, 6, 12, 12, 5)
        self.item = loc._normalize_item(_raw_item(), loc.FOCUS_AREAS[0])
        for patcher in (
            mock.patch.object(loc, "_fetch_items", return_value=[self.item]),
            mock.patch.object(loc.summarize, "brief", return_value=STORY),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pushes_once_per_window_with_image_attachment(self):
        with capture_pushes() as sent:
            state = loc._run({}, self.now)
            loc._run(state, self.now + _dt.timedelta(minutes=30))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "Library of Congress: March on Washington photograph")
        self.assertEqual(sent[0]["message"], STORY)
        self.assertEqual(sent[0]["attach_url"], "https://www.loc.gov/static/images/sample.jpg")
        self.assertEqual(sent[0]["click_url"], "https://www.loc.gov/item/123/")
        self.assertEqual(state[loc.LOC_SENT_KEY], loc._window(self.now))
        self.assertIn("https://www.loc.gov/item/123/", state[loc.LOC_SEEN_KEY])

    def test_pushes_without_image_when_none_available(self):
        no_image = loc._normalize_item(_raw_item(image_url=None), loc.FOCUS_AREAS[0])
        with mock.patch.object(loc, "_fetch_items", return_value=[no_image]), \
             capture_pushes() as sent:
            loc._run({}, self.now)
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0]["attach_url"])

    def test_fetch_failure_skips_without_stamping(self):
        with mock.patch.object(loc, "_fetch_items", side_effect=RuntimeError("boom")), \
             capture_pushes() as sent:
            state = loc._run({}, self.now)
        self.assertEqual(sent, [])
        self.assertNotIn(loc.LOC_SENT_KEY, state)
        self.assertNotIn(loc.LOC_SEEN_KEY, state)

    def test_gemini_failure_skips_without_consuming_item(self):
        with mock.patch.object(loc.summarize, "brief", return_value=None), \
             capture_pushes() as sent:
            state = loc._run({}, self.now)
        self.assertEqual(sent, [])
        self.assertNotIn(loc.LOC_SENT_KEY, state)
        self.assertNotIn(loc.LOC_SEEN_KEY, state)

    def test_next_window_rotates_focus(self):
        def items_for(focus: str) -> list[dict]:
            return [loc._normalize_item(
                _raw_item(item_id=f"https://www.loc.gov/item/{focus}/",
                          title=f"{focus} item"),
                focus,
            )]

        with mock.patch.object(loc, "_fetch_items", side_effect=items_for), \
             capture_pushes() as sent:
            state = loc._run({}, self.now)
            loc._run(state, self.now + _dt.timedelta(hours=loc.LOC_WINDOW_HOURS))
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["title"], sent[1]["title"])
        self.assertEqual(state[loc.LOC_FOCUS_KEY], loc.FOCUS_AREAS[1])

    def test_run_entry_point_is_not_daily_gated(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = loc.run({})
        self.assertEqual(len(sent), 1)
        self.assertIn(loc.LOC_SENT_KEY, state)


class IntegrationContractTest(unittest.TestCase):
    def test_topic_is_registered(self):
        from notify_watcher import main

        self.assertIn(loc.TOPIC, [name for name, _ in main.TOPICS])

    def test_live_priority_config_pushes_topic(self):
        decision = priority.decide(_event(), config.section("priority"))
        self.assertEqual(decision.action, "push")
        self.assertEqual(decision.score, 60)


if __name__ == "__main__":
    unittest.main()
