"""Tests for the "Knowledge" channel of the daily learning push.

Covers the two behaviors the channel adds over the plain day-of-year KBs:
even cyclic rotation across categories, and a no-repeat memory window stamped
into state. All tests run on synthetic in-memory KBs (no network, no LLM);
the golden test at the end validates the real data/knowledge.json.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher import kb
from notify_watcher.topics import learn


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


class CategoryRotationTest(unittest.TestCase):
    """The category pointer cycles so picks never cluster on one topic."""

    def test_categories_rotate_evenly(self):
        cats = ["alpha", "bravo", "charlie", "delta"]
        entries = _fake_kb(cats, per_category=10)
        state: dict = {}
        seen_cats: list[str] = []
        day = _dt.date(2026, 1, 1)
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            for offset in range(len(cats) * 3):
                learn._knowledge_fact(state, day + _dt.timedelta(days=offset))
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
            learn._knowledge_fact(state, _dt.date(2026, 5, 1))
        self.assertEqual(state[learn.KNOWLEDGE_CATEGORY_KEY], "charlie")

    def test_unknown_state_category_starts_from_first(self):
        cats = ["alpha", "bravo"]
        entries = _fake_kb(cats, per_category=3)
        state = {learn.KNOWLEDGE_CATEGORY_KEY: "deleted_category"}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            learn._knowledge_fact(state, _dt.date(2026, 5, 1))
        self.assertEqual(state[learn.KNOWLEDGE_CATEGORY_KEY], "alpha")

    def test_exhausted_category_is_skipped(self):
        cats = ["alpha", "bravo"]
        entries = _fake_kb(cats, per_category=1)
        day = _dt.date(2026, 5, 1)
        # alpha's only entry was shown yesterday -> today must serve bravo.
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "alpha_0": (day - _dt.timedelta(days=1)).isoformat()
            },
            learn.KNOWLEDGE_CATEGORY_KEY: "bravo",  # rotation would start at alpha
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            title, _ = learn._knowledge_fact(state, day)
        self.assertEqual(title, "Title bravo 0")


class DeduplicationTest(unittest.TestCase):
    """No entry repeats within the KNOWLEDGE_REPEAT_DAYS window."""

    def test_no_repeats_within_window(self):
        entries = _fake_kb(["a", "b", "c", "d", "e"], per_category=8)  # 40 > 30
        state: dict = {}
        picked: list[str] = []
        day = _dt.date(2026, 1, 1)
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            for offset in range(learn.KNOWLEDGE_REPEAT_DAYS):
                before = {learn.KNOWLEDGE_SEEN_KEY:
                          dict(state.get(learn.KNOWLEDGE_SEEN_KEY) or {})}
                learn._knowledge_fact(state, day + _dt.timedelta(days=offset))
                picked.append(_pick_id(before, state))
        self.assertEqual(len(picked), len(set(picked)),
                         "an entry repeated inside the no-repeat window")

    def test_entry_eligible_again_after_window(self):
        entries = _fake_kb(["solo"], per_category=1)
        day = _dt.date(2026, 6, 1)
        stale = day - _dt.timedelta(days=learn.KNOWLEDGE_REPEAT_DAYS)
        state = {learn.KNOWLEDGE_SEEN_KEY: {"solo_0": stale.isoformat()}}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            title, body = learn._knowledge_fact(state, day)
        self.assertEqual(title, "Title solo 0")
        self.assertEqual(state[learn.KNOWLEDGE_SEEN_KEY]["solo_0"], day.isoformat())

    def test_all_seen_falls_back_to_least_recent(self):
        entries = _fake_kb(["a", "b"], per_category=1)
        day = _dt.date(2026, 6, 10)
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "a_0": (day - _dt.timedelta(days=5)).isoformat(),   # older
                "b_0": (day - _dt.timedelta(days=2)).isoformat(),
            }
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            title, body = learn._knowledge_fact(state, day)
        self.assertEqual(title, "Title a 0", "should reuse the least recently shown")
        self.assertTrue(body)  # the section never goes silent

    def test_expired_and_malformed_stamps_are_pruned(self):
        entries = _fake_kb(["a"], per_category=3)
        day = _dt.date(2026, 6, 10)
        fresh = (day - _dt.timedelta(days=3)).isoformat()
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "a_0": (day - _dt.timedelta(days=200)).isoformat(),  # expired
                "a_1": "not-a-date",                                 # malformed
                "a_2": fresh,                                        # still fresh
            }
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            learn._knowledge_fact(state, day)
        seen = state[learn.KNOWLEDGE_SEEN_KEY]
        picked = [i for i, stamp in seen.items() if stamp == day.isoformat()]
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], {"a_0", "a_1"})  # a_2 was still ineligible
        # Only the fresh stamp and today's pick survive; expired and malformed
        # stamps are gone.
        self.assertEqual(seen, {"a_2": fresh, picked[0]: day.isoformat()})

    def test_same_day_rerun_picks_same_entry(self):
        # Re-run safety: the runner re-executing the same date must not drift.
        entries = _fake_kb(["a", "b", "c"], per_category=6)
        day = _dt.date(2026, 7, 4)
        base = {
            learn.KNOWLEDGE_SEEN_KEY: {"a_1": (day - _dt.timedelta(days=4)).isoformat()},
            learn.KNOWLEDGE_CATEGORY_KEY: "c",
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            first = learn._knowledge_fact(dict(base), day)
            second = learn._knowledge_fact(dict(base), day)
        self.assertEqual(first, second)

    def test_empty_kb_returns_nothing(self):
        with mock.patch.object(learn, "_knowledge_entries", return_value=[]):
            self.assertEqual(learn._knowledge_fact({}, _dt.date(2026, 1, 1)), ("", ""))


class WiringTest(unittest.TestCase):
    """The channel is wired into the learn rotation like the others."""

    def test_knowledge_is_a_rotation_channel(self):
        self.assertIn((learn.KNOWLEDGE_LABEL, learn.KNOWLEDGE_FILE), learn.CHANNELS)

    def test_curated_fact_routes_to_knowledge_with_title_header(self):
        idx = [label for label, _ in learn.CHANNELS].index(learn.KNOWLEDGE_LABEL)
        # Jan 1 has day-of-year 1, so this date's rotation index is exactly idx.
        day = _dt.date(2026, 1, 1) + _dt.timedelta(days=(idx - 1) % len(learn.CHANNELS))
        self.assertEqual(kb.day_of_year(day) % len(learn.CHANNELS), idx)

        entries = _fake_kb(["only"], per_category=1)
        state: dict = {}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries), \
             mock.patch.object(learn.summarize, "one_line",
                               side_effect=AssertionError("knowledge must not be reworded")):
            header, body = learn._curated_fact(day, state)
        self.assertEqual(header, "Title only 0")  # entry title, not the channel label
        self.assertEqual(body, "Body only 0.")    # verbatim body
        self.assertIn("only_0", state[learn.KNOWLEDGE_SEEN_KEY])


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
