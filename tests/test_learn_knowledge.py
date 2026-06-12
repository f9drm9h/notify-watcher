"""Tests for the standalone "Knowledge" push of notify_watcher.topics.learn.

One titled entry from data/knowledge.json goes out on EVERY watcher run
(every 3 hours), independent of the NOTIFY_DAILY gate. Covers the behaviors
that distinguish it from the plain day-of-year KB channels: even cyclic
rotation across categories, the 30-day no-repeat memory window stamped into
state, the per-3-hour-window double-send guard, and window-seeded re-run
determinism. All tests run on synthetic in-memory KBs (no network, no LLM);
the golden test at the end validates the real data/knowledge.json.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import learn
from tests._util import capture_pushes


def _fake_kb(categories: list[str], per_category: int) -> list[dict]:
    """A synthetic knowledge KB with `per_category` entries per category."""
    return [
        {
            "id": f"{cat}_{i}",
            "category": cat,
            "title": f"Title {cat} {i}",
            "body": f"Body {cat} {i}.",
            "tags": [cat],
        }
        for cat in categories
        for i in range(per_category)
    ]


def _pick_id(state_before: dict, state_after: dict) -> str:
    """The id newly stamped (or re-stamped today) by a _knowledge_fact call."""
    before = dict(state_before.get(learn.KNOWLEDGE_SEEN_KEY) or {})
    after = state_after[learn.KNOWLEDGE_SEEN_KEY]
    changed = [i for i, d in after.items() if before.get(i) != d]
    assert len(changed) == 1, f"expected exactly one new stamp, got {changed}"
    return changed[0]


def _runs(start: _dt.datetime, count: int) -> list[_dt.datetime]:
    """`count` consecutive 3-hour cron firings starting at `start`."""
    step = _dt.timedelta(hours=learn.KNOWLEDGE_WINDOW_HOURS)
    return [start + step * i for i in range(count)]


class WindowTest(unittest.TestCase):
    """The 3-hour-window stamp that seeds the pick and guards re-runs."""

    def test_hours_bucket_into_3h_windows(self):
        day = "2026-06-16"
        self.assertEqual(learn._knowledge_window(_dt.datetime(2026, 6, 16, 0, 0)),
                         f"{day}T0")
        self.assertEqual(learn._knowledge_window(_dt.datetime(2026, 6, 16, 2, 59)),
                         f"{day}T0")  # drift inside a window keeps the stamp
        self.assertEqual(learn._knowledge_window(_dt.datetime(2026, 6, 16, 3, 0)),
                         f"{day}T1")
        self.assertEqual(learn._knowledge_window(_dt.datetime(2026, 6, 16, 23, 59)),
                         f"{day}T7")

    def test_windows_differ_across_days(self):
        self.assertNotEqual(learn._knowledge_window(_dt.datetime(2026, 6, 16, 1)),
                            learn._knowledge_window(_dt.datetime(2026, 6, 17, 1)))


class CategoryRotationTest(unittest.TestCase):
    """The category pointer cycles so picks never cluster on one topic."""

    def test_categories_rotate_evenly_across_runs(self):
        cats = ["alpha", "bravo", "charlie", "delta"]
        entries = _fake_kb(cats, per_category=10)
        state: dict = {}
        seen_cats: list[str] = []
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            for now in _runs(_dt.datetime(2026, 1, 1, 0, 30), len(cats) * 3):
                learn._knowledge_fact(state, now)
                seen_cats.append(state[learn.KNOWLEDGE_CATEGORY_KEY])
        # Every window of len(cats) consecutive picks covers every category once.
        for i in range(0, len(seen_cats), len(cats)):
            window = seen_cats[i:i + len(cats)]
            self.assertEqual(sorted(window), sorted(cats),
                             f"picks clustered: {seen_cats}")

    def test_rotation_starts_after_state_category(self):
        cats = ["alpha", "bravo", "charlie"]
        entries = _fake_kb(cats, per_category=5)
        state = {learn.KNOWLEDGE_CATEGORY_KEY: "bravo"}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            learn._knowledge_fact(state, _dt.datetime(2026, 5, 1, 9))
        self.assertEqual(state[learn.KNOWLEDGE_CATEGORY_KEY], "charlie")

    def test_unknown_state_category_starts_from_first(self):
        cats = ["alpha", "bravo"]
        entries = _fake_kb(cats, per_category=3)
        state = {learn.KNOWLEDGE_CATEGORY_KEY: "deleted_category"}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            learn._knowledge_fact(state, _dt.datetime(2026, 5, 1, 9))
        self.assertEqual(state[learn.KNOWLEDGE_CATEGORY_KEY], "alpha")

    def test_exhausted_category_is_skipped(self):
        cats = ["alpha", "bravo"]
        entries = _fake_kb(cats, per_category=1)
        now = _dt.datetime(2026, 5, 1, 9)
        # alpha's only entry was shown yesterday -> this run must serve bravo.
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "alpha_0": (now.date() - _dt.timedelta(days=1)).isoformat()
            },
            learn.KNOWLEDGE_CATEGORY_KEY: "bravo",  # rotation would start at alpha
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            title, _ = learn._knowledge_fact(state, now)
        self.assertEqual(title, "Title bravo 0")


class DeduplicationTest(unittest.TestCase):
    """No entry repeats within the KNOWLEDGE_REPEAT_DAYS window."""

    def test_no_repeats_across_consecutive_runs(self):
        # 8 runs/day for ~5 days = 39 picks; the 40-entry KB must not repeat.
        entries = _fake_kb(["a", "b", "c", "d", "e"], per_category=8)
        state: dict = {}
        picked: list[str] = []
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            for now in _runs(_dt.datetime(2026, 1, 1, 1, 0), 39):
                before = {learn.KNOWLEDGE_SEEN_KEY:
                          dict(state.get(learn.KNOWLEDGE_SEEN_KEY) or {})}
                learn._knowledge_fact(state, now)
                picked.append(_pick_id(before, state))
        self.assertEqual(len(picked), len(set(picked)),
                         "an entry repeated inside the no-repeat window")

    def test_entry_eligible_again_after_window(self):
        entries = _fake_kb(["solo"], per_category=1)
        now = _dt.datetime(2026, 6, 1, 12)
        stale = now.date() - _dt.timedelta(days=learn.KNOWLEDGE_REPEAT_DAYS)
        state = {learn.KNOWLEDGE_SEEN_KEY: {"solo_0": stale.isoformat()}}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            title, body = learn._knowledge_fact(state, now)
        self.assertEqual(title, "Title solo 0")
        self.assertEqual(state[learn.KNOWLEDGE_SEEN_KEY]["solo_0"],
                         now.date().isoformat())

    def test_all_seen_falls_back_to_least_recent(self):
        entries = _fake_kb(["a", "b"], per_category=1)
        now = _dt.datetime(2026, 6, 10, 6)
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "a_0": (now.date() - _dt.timedelta(days=5)).isoformat(),   # older
                "b_0": (now.date() - _dt.timedelta(days=2)).isoformat(),
            }
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            title, body = learn._knowledge_fact(state, now)
        self.assertEqual(title, "Title a 0", "should reuse the least recently shown")
        self.assertTrue(body)  # the push never goes silent

    def test_expired_and_malformed_stamps_are_pruned(self):
        entries = _fake_kb(["a"], per_category=3)
        now = _dt.datetime(2026, 6, 10, 15)
        fresh = (now.date() - _dt.timedelta(days=3)).isoformat()
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "a_0": (now.date() - _dt.timedelta(days=200)).isoformat(),  # expired
                "a_1": "not-a-date",                                        # malformed
                "a_2": fresh,                                               # still fresh
            }
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            learn._knowledge_fact(state, now)
        seen = state[learn.KNOWLEDGE_SEEN_KEY]
        picked = [i for i, stamp in seen.items() if stamp == now.date().isoformat()]
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], {"a_0", "a_1"})  # a_2 was still ineligible
        # Only the fresh stamp and this run's pick survive; expired and
        # malformed stamps are gone.
        self.assertEqual(seen, {"a_2": fresh, picked[0]: now.date().isoformat()})

    def test_same_window_rerun_picks_same_entry(self):
        # Re-run safety: the runner re-executing inside one 3-hour window
        # (same window stamp, even a different minute) must not drift.
        entries = _fake_kb(["a", "b", "c"], per_category=6)
        base = {
            learn.KNOWLEDGE_SEEN_KEY: {"a_1": "2026-06-30"},
            learn.KNOWLEDGE_CATEGORY_KEY: "c",
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            first = learn._knowledge_fact(dict(base), _dt.datetime(2026, 7, 4, 12, 5))
            second = learn._knowledge_fact(dict(base), _dt.datetime(2026, 7, 4, 13, 59))
        self.assertEqual(first, second)

    def test_consecutive_windows_pick_distinct_entries(self):
        entries = _fake_kb(["a", "b", "c"], per_category=6)
        state: dict = {}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            first = learn._knowledge_fact(state, _dt.datetime(2026, 7, 4, 12, 5))
            second = learn._knowledge_fact(state, _dt.datetime(2026, 7, 4, 15, 5))
        self.assertNotEqual(first, second)

    def test_empty_kb_returns_nothing(self):
        with mock.patch.object(learn, "_knowledge_entries", return_value=[]):
            self.assertEqual(
                learn._knowledge_fact({}, _dt.datetime(2026, 1, 1, 0)), ("", ""))


class RunKnowledgeTest(unittest.TestCase):
    """The push fires once per 3-hour window, independent of the daily gate."""

    def setUp(self):
        self.entries = _fake_kb(["a", "b", "c"], per_category=4)
        patcher = mock.patch.object(
            learn, "_knowledge_entries", return_value=self.entries)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pushes_once_per_window_with_title_header(self):
        now = _dt.datetime(2026, 6, 16, 12, 1)
        with capture_pushes() as sent:
            state = learn._run_knowledge({}, now)
            learn._run_knowledge(state, now + _dt.timedelta(minutes=30))  # re-run
        self.assertEqual(len(sent), 1, "a re-run inside the window must not resend")
        self.assertRegex(sent[0]["title"], r"^Title ")   # entry title is the header
        self.assertRegex(sent[0]["message"], r"^Body ")  # verbatim body
        self.assertEqual(state[learn.KNOWLEDGE_SENT_KEY],
                         learn._knowledge_window(now))

    def test_pushes_again_next_window(self):
        now = _dt.datetime(2026, 6, 16, 9, 0)
        with capture_pushes() as sent:
            state = learn._run_knowledge({}, now)
            learn._run_knowledge(
                state, now + _dt.timedelta(hours=learn.KNOWLEDGE_WINDOW_HOURS))
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["title"], sent[1]["title"],
                            "consecutive windows must serve distinct entries")

    def test_body_is_never_reworded(self):
        with mock.patch.object(
                learn.summarize, "one_line",
                side_effect=AssertionError("knowledge must not be reworded")), \
             capture_pushes():
            learn._run_knowledge({}, _dt.datetime(2026, 6, 16, 18, 2))

    def test_run_fires_knowledge_without_daily_gate(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = learn.run({})
        self.assertEqual(len(sent), 1)  # knowledge only; no daily learning push
        self.assertRegex(sent[0]["title"], r"^Title ")
        self.assertIn(learn.KNOWLEDGE_SENT_KEY, state)
        self.assertNotIn(learn.STATE_KEY, state)

    def test_daily_run_sends_knowledge_and_consolidated_push(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": "1"}, clear=False), \
             mock.patch.object(learn, "_fetch_feed", return_value={}), \
             mock.patch.object(learn.summarize, "one_line", return_value=None), \
             capture_pushes() as sent:
            state = learn.run({})
        self.assertEqual(len(sent), 2)
        self.assertRegex(sent[0]["title"], r"^Title ")        # knowledge first
        self.assertEqual(sent[1]["title"], "Daily learning")  # then the daily push
        self.assertIn(learn.KNOWLEDGE_SENT_KEY, state)
        self.assertEqual(state[learn.STATE_KEY], _dt.date.today().isoformat())

    def test_knowledge_is_not_in_the_daily_rotation(self):
        self.assertNotIn(learn.KNOWLEDGE_FILE,
                         [filename for _, filename in learn.CHANNELS])


class KnowledgeDataTest(unittest.TestCase):
    """Golden test over the real data/knowledge.json, mirroring ChannelsTest in
    test_learn.py: a malformed or shrunken KB fails CI instead of silently
    weakening the channel."""

    CATEGORIES = {
        "early_humans", "science", "astronomy", "medicine", "technology",
        "ancient_civilizations", "mythology", "philosophy", "mathematics",
        "world_history",
    }

    def test_knowledge_kb_is_well_formed(self):
        entries = learn._knowledge_entries()
        self.assertGreaterEqual(len(entries), 80)

        ids = [str(e["id"]) for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "entry ids must be unique")

        per_category: dict[str, int] = {}
        for e in entries:
            self.assertTrue(str(e.get("title", "")).strip(), e["id"])
            self.assertTrue(str(e.get("body", "")).strip(), e["id"])
            self.assertIn(e["category"], self.CATEGORIES, e["id"])
            tags = e.get("tags")
            self.assertIsInstance(tags, list, e["id"])
            self.assertTrue(tags and all(isinstance(t, str) and t for t in tags),
                            e["id"])
            per_category[e["category"]] = per_category.get(e["category"], 0) + 1

        self.assertEqual(set(per_category), self.CATEGORIES,
                         "every category must be populated")
        for category, count in per_category.items():
            self.assertGreaterEqual(count, 6, f"{category} is too thin")


if __name__ == "__main__":
    unittest.main()
