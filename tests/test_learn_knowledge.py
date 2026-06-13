"""Tests for the Gemini-powered "Knowledge" story push of notify_watcher.topics.learn.

On EVERY watcher run (every 3 hours, independent of NOTIFY_DAILY) one topic is
chosen from data/knowledge_topics.json — category rotation + a 30-day no-repeat
window, window-seeded for re-run determinism — and narrated fresh by Gemini
(summarize.brief). The topic is recorded and the window stamped ONLY after a
story comes back, so a generation failure skips cleanly and retries next run.
All tests run on synthetic in-memory KBs with the LLM mocked (no network); the
golden test at the end validates the real data/knowledge_topics.json.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import learn
from tests._util import capture_pushes

STORY = ("In a distant age, remarkable people set events in motion. "
         "Their choices echoed for generations. The world was never the same. "
         "And so the story endures, retold in wonder.")


def _fake_kb(categories: list[str], per_category: int) -> list[dict]:
    """A synthetic topic KB with `per_category` topics per category."""
    return [
        {"id": f"{cat}:Title {cat} {i}", "category": cat, "title": f"Title {cat} {i}"}
        for cat in categories
        for i in range(per_category)
    ]


def _pick_and_commit(state: dict, now: _dt.datetime) -> dict | None:
    """Select a topic and record it — the selection half of _run_knowledge."""
    chosen = learn._knowledge_pick(state, now)
    if chosen is not None:
        learn._knowledge_commit(state, chosen, now.date())
    return chosen


def _pick_id(state_before: dict, state_after: dict) -> str:
    """The id newly stamped (or re-stamped today) by a pick+commit."""
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
                _pick_and_commit(state, now)
                seen_cats.append(state[learn.KNOWLEDGE_CATEGORY_KEY])
        for i in range(0, len(seen_cats), len(cats)):
            window = seen_cats[i:i + len(cats)]
            self.assertEqual(sorted(window), sorted(cats),
                             f"picks clustered: {seen_cats}")

    def test_rotation_starts_after_state_category(self):
        cats = ["alpha", "bravo", "charlie"]
        entries = _fake_kb(cats, per_category=5)
        state = {learn.KNOWLEDGE_CATEGORY_KEY: "bravo"}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            _pick_and_commit(state, _dt.datetime(2026, 5, 1, 9))
        self.assertEqual(state[learn.KNOWLEDGE_CATEGORY_KEY], "charlie")

    def test_unknown_state_category_starts_from_first(self):
        cats = ["alpha", "bravo"]
        entries = _fake_kb(cats, per_category=3)
        state = {learn.KNOWLEDGE_CATEGORY_KEY: "deleted_category"}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            _pick_and_commit(state, _dt.datetime(2026, 5, 1, 9))
        self.assertEqual(state[learn.KNOWLEDGE_CATEGORY_KEY], "alpha")

    def test_exhausted_category_is_skipped(self):
        cats = ["alpha", "bravo"]
        entries = _fake_kb(cats, per_category=1)
        now = _dt.datetime(2026, 5, 1, 9)
        # alpha's only topic was shown yesterday -> this run must serve bravo.
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "alpha:Title alpha 0": (now.date() - _dt.timedelta(days=1)).isoformat()
            },
            learn.KNOWLEDGE_CATEGORY_KEY: "bravo",  # rotation would start at alpha
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["title"], "Title bravo 0")


class DeduplicationTest(unittest.TestCase):
    """No topic repeats within the KNOWLEDGE_REPEAT_DAYS window."""

    def test_no_repeats_across_consecutive_runs(self):
        # 8 runs/day for ~5 days = 39 picks; the 40-topic KB must not repeat.
        entries = _fake_kb(["a", "b", "c", "d", "e"], per_category=8)
        state: dict = {}
        picked: list[str] = []
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            for now in _runs(_dt.datetime(2026, 1, 1, 1, 0), 39):
                before = {learn.KNOWLEDGE_SEEN_KEY:
                          dict(state.get(learn.KNOWLEDGE_SEEN_KEY) or {})}
                _pick_and_commit(state, now)
                picked.append(_pick_id(before, state))
        self.assertEqual(len(picked), len(set(picked)),
                         "a topic repeated inside the no-repeat window")

    def test_topic_eligible_again_after_window(self):
        entries = _fake_kb(["solo"], per_category=1)
        now = _dt.datetime(2026, 6, 1, 12)
        stale = now.date() - _dt.timedelta(days=learn.KNOWLEDGE_REPEAT_DAYS)
        tid = "solo:Title solo 0"
        state = {learn.KNOWLEDGE_SEEN_KEY: {tid: stale.isoformat()}}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["title"], "Title solo 0")
        self.assertEqual(state[learn.KNOWLEDGE_SEEN_KEY][tid], now.date().isoformat())

    def test_all_seen_falls_back_to_least_recent(self):
        entries = _fake_kb(["a", "b"], per_category=1)
        now = _dt.datetime(2026, 6, 10, 6)
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "a:Title a 0": (now.date() - _dt.timedelta(days=5)).isoformat(),  # older
                "b:Title b 0": (now.date() - _dt.timedelta(days=2)).isoformat(),
            }
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["title"], "Title a 0",
                         "should reuse the least recently shown topic")

    def test_expired_and_malformed_stamps_are_pruned(self):
        entries = _fake_kb(["a"], per_category=3)
        now = _dt.datetime(2026, 6, 10, 15)
        fresh = (now.date() - _dt.timedelta(days=3)).isoformat()
        state = {
            learn.KNOWLEDGE_SEEN_KEY: {
                "a:Title a 0": (now.date() - _dt.timedelta(days=200)).isoformat(),  # expired
                "a:Title a 1": "not-a-date",                                        # malformed
                "a:Title a 2": fresh,                                               # still fresh
            }
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            _pick_and_commit(state, now)
        seen = state[learn.KNOWLEDGE_SEEN_KEY]
        picked = [i for i, stamp in seen.items() if stamp == now.date().isoformat()]
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], {"a:Title a 0", "a:Title a 1"})  # a 2 still ineligible
        self.assertEqual(seen, {"a:Title a 2": fresh,
                                picked[0]: now.date().isoformat()})

    def test_same_window_rerun_picks_same_topic(self):
        # Re-run safety: the runner re-executing inside one 3-hour window
        # (same window stamp, even a different minute) must not drift.
        entries = _fake_kb(["a", "b", "c"], per_category=6)
        base = {
            learn.KNOWLEDGE_SEEN_KEY: {"a:Title a 1": "2026-06-30"},
            learn.KNOWLEDGE_CATEGORY_KEY: "c",
        }
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            first = learn._knowledge_pick(dict(base), _dt.datetime(2026, 7, 4, 12, 5))
            second = learn._knowledge_pick(dict(base), _dt.datetime(2026, 7, 4, 13, 59))
        self.assertEqual(first, second)

    def test_consecutive_windows_pick_distinct_topics(self):
        entries = _fake_kb(["a", "b", "c"], per_category=6)
        state: dict = {}
        with mock.patch.object(learn, "_knowledge_entries", return_value=entries):
            first = _pick_and_commit(state, _dt.datetime(2026, 7, 4, 12, 5))
            second = _pick_and_commit(state, _dt.datetime(2026, 7, 4, 15, 5))
        self.assertNotEqual(first["id"], second["id"])

    def test_empty_kb_returns_none(self):
        with mock.patch.object(learn, "_knowledge_entries", return_value=[]):
            self.assertIsNone(
                learn._knowledge_pick({}, _dt.datetime(2026, 1, 1, 0)))


class StoryGenerationTest(unittest.TestCase):
    """_generate_story delegates to Gemini (summarize.brief) and clips the result."""

    def test_passes_topic_into_the_prompt(self):
        with mock.patch.object(learn.summarize, "brief", return_value=STORY) as brief:
            learn._generate_story("The fall of the Berlin Wall")
        brief.assert_called_once()
        # call signature: brief(system_instruction, user_prompt, max_tokens=...)
        user_prompt = brief.call_args.args[1]
        self.assertIn("The fall of the Berlin Wall", user_prompt)
        self.assertIn("four substantial paragraphs", user_prompt)

    def test_none_when_no_provider(self):
        with mock.patch.object(learn.summarize, "brief", return_value=None):
            self.assertIsNone(learn._generate_story("Anything"))

    def test_long_story_is_clipped_to_fit_ntfy(self):
        with mock.patch.object(learn.summarize, "brief",
                               return_value="A sentence here. " * 4000):
            out = learn._generate_story("Anything")
        self.assertLessEqual(len(out), learn.KNOWLEDGE_CLIP_CHARS)


class ClipStoryTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(learn._clip_story("Just a line."), "Just a line.")

    def test_clips_on_a_sentence_boundary(self):
        text = "First sentence here. Second sentence here. " * 10
        out = learn._clip_story(text, limit=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("."), out)

    def test_falls_back_to_word_boundary_without_sentences(self):
        out = learn._clip_story("word " * 50, limit=20)
        self.assertLessEqual(len(out), 21)  # may add the ellipsis
        self.assertTrue(out.endswith("…"))


class RunKnowledgeTest(unittest.TestCase):
    """The push fires once per 3-hour window, narrated by Gemini."""

    def setUp(self):
        self.entries = _fake_kb(["a", "b", "c"], per_category=4)
        p1 = mock.patch.object(
            learn, "_knowledge_entries", return_value=self.entries)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(learn.summarize, "brief", return_value=STORY)
        p2.start()
        self.addCleanup(p2.stop)

    def test_pushes_once_per_window_with_topic_header_and_story_body(self):
        now = _dt.datetime(2026, 6, 16, 12, 1)
        with capture_pushes() as sent:
            state = learn._run_knowledge({}, now)
            learn._run_knowledge(state, now + _dt.timedelta(minutes=30))  # re-run
        self.assertEqual(len(sent), 1, "a re-run inside the window must not resend")
        self.assertRegex(sent[0]["title"], r"^Title ")   # topic title is the header
        self.assertEqual(sent[0]["message"], STORY)      # the generated narrative
        self.assertEqual(state[learn.KNOWLEDGE_SENT_KEY],
                         learn._knowledge_window(now))

    def test_pushes_again_next_window_with_distinct_topic(self):
        now = _dt.datetime(2026, 6, 16, 9, 0)
        with capture_pushes() as sent:
            state = learn._run_knowledge({}, now)
            learn._run_knowledge(
                state, now + _dt.timedelta(hours=learn.KNOWLEDGE_WINDOW_HOURS))
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["title"], sent[1]["title"],
                            "consecutive windows must serve distinct topics")

    def test_gemini_failure_skips_cleanly_and_retries(self):
        now = _dt.datetime(2026, 6, 16, 18, 2)
        with mock.patch.object(learn.summarize, "brief", return_value=None), \
             capture_pushes() as sent:
            state = learn._run_knowledge({}, now)
        self.assertEqual(sent, [], "no push when the story can't be generated")
        # Neither the window nor the topic is consumed, so the next run retries.
        self.assertNotIn(learn.KNOWLEDGE_SENT_KEY, state)
        self.assertNotIn(learn.KNOWLEDGE_SEEN_KEY, state)

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
    """Golden test over the real data/knowledge_topics.json: a malformed or
    shrunken KB fails CI instead of silently weakening the channel."""

    CATEGORIES = {
        "early_humans", "science", "astronomy", "medicine", "technology",
        "ancient_civilizations", "mythology", "philosophy", "math_history",
        "world_history",
    }

    def test_topics_kb_is_well_formed(self):
        entries = learn._knowledge_entries()
        self.assertGreaterEqual(len(entries), 500, "expected 500+ topics")

        ids = [str(e["id"]) for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "topic ids must be unique")

        per_category: dict[str, int] = {}
        for e in entries:
            self.assertTrue(str(e.get("title", "")).strip(), e["id"])
            self.assertIn(e["category"], self.CATEGORIES, e["id"])
            per_category[e["category"]] = per_category.get(e["category"], 0) + 1

        self.assertEqual(set(per_category), self.CATEGORIES,
                         "every category must be populated")
        for category, count in per_category.items():
            self.assertGreaterEqual(count, 50, f"{category} has only {count} topics")


if __name__ == "__main__":
    unittest.main()
