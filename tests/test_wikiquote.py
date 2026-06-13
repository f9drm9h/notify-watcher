"""Tests for the Gemini-narrated Wikiquote story push (notify_watcher.topics.wikiquote).

On EVERY watcher run (every 3 hours, independent of NOTIFY_DAILY) one figure is
chosen from the curated FIGURES list — category rotation + a 30-day no-repeat
window, window-seeded for re-run determinism — a REAL quote is fetched from
Wikiquote, and Gemini (summarize.brief) narrates a story around it. The figure
is recorded and the window stamped ONLY after both the quote fetch and the story
succeed, so any failure skips cleanly and retries next run. The Wikiquote API and
the LLM are mocked throughout (no network); the golden test validates the real
FIGURES list.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import wikiquote
from tests._util import capture_pushes

STORY = ("In a distant age, a remarkable mind set ideas in motion. "
         "The words endured long after the speaker was gone. "
         "And so the quote still echoes, retold in wonder.")

QUOTES = ["Imagination is more important than knowledge for the curious mind.",
          "Try not to become a person of success but rather of value to others.",
          "The important thing is not to stop questioning every single day."]

# A trimmed-down but realistic Wikiquote page wikitext: a Quotes section with
# top-level bullets (quotes) and sub-bullets (sources), then a "Quotes about"
# section whose bullets must be ignored.
SAMPLE_WIKITEXT = """== Quotes ==
* Imagination is more important than [[knowledge]] for the curious mind.<ref>A letter, 1929.</ref>
** Source: some interview
* Try not to become a person of success but rather of '''value''' to others.
*: a continuation that is not its own quote
* Short one.
* The important thing is not to stop questioning every single day.{{Citation needed}}

== Quotes about the figure ==
* This bullet is someone else praising them and must be skipped entirely here.
"""


def _runs(start: _dt.datetime, count: int) -> list[_dt.datetime]:
    """`count` consecutive 3-hour cron firings starting at `start`."""
    step = _dt.timedelta(hours=wikiquote.WIKIQUOTE_WINDOW_HOURS)
    return [start + step * i for i in range(count)]


def _fake_figures(categories: list[str], per_category: int) -> dict[str, list[str]]:
    """A synthetic FIGURES map with `per_category` names per category."""
    return {cat: [f"{cat} {i}" for i in range(per_category)] for cat in categories}


def _pick_and_commit(state: dict, now: _dt.datetime) -> dict | None:
    """Select a figure and record it — the selection half of run()."""
    chosen = wikiquote._pick_figure(state, now)
    if chosen is not None:
        wikiquote._commit(state, chosen, now.date())
    return chosen


def _pick_id(state_before: dict, state_after: dict) -> str:
    """The id newly stamped (or re-stamped today) by a pick+commit."""
    before = dict(state_before.get(wikiquote.WIKIQUOTE_SEEN_KEY) or {})
    after = state_after[wikiquote.WIKIQUOTE_SEEN_KEY]
    changed = [i for i, d in after.items() if before.get(i) != d]
    assert len(changed) == 1, f"expected exactly one new stamp, got {changed}"
    return changed[0]


class WindowTest(unittest.TestCase):
    """The 3-hour-window stamp that seeds the pick and guards re-runs."""

    def test_hours_bucket_into_3h_windows(self):
        day = "2026-06-16"
        self.assertEqual(wikiquote._window(_dt.datetime(2026, 6, 16, 0, 0)), f"{day}T0")
        self.assertEqual(wikiquote._window(_dt.datetime(2026, 6, 16, 2, 59)), f"{day}T0")
        self.assertEqual(wikiquote._window(_dt.datetime(2026, 6, 16, 3, 0)), f"{day}T1")
        self.assertEqual(wikiquote._window(_dt.datetime(2026, 6, 16, 23, 59)), f"{day}T7")

    def test_windows_differ_across_days(self):
        self.assertNotEqual(wikiquote._window(_dt.datetime(2026, 6, 16, 1)),
                            wikiquote._window(_dt.datetime(2026, 6, 17, 1)))


class ExtractQuotesTest(unittest.TestCase):
    """The wikitext parser keeps the subject's own quotes and cleans markup."""

    def test_extracts_top_level_quotes_only(self):
        quotes = wikiquote._extract_quotes(SAMPLE_WIKITEXT)
        # Two long-enough top-level quotes survive; the "Short one." bullet is
        # below the word/char floor, and sub-bullets are never quotes.
        self.assertIn("Imagination is more important than knowledge for the curious mind.",
                      quotes)
        self.assertIn("Try not to become a person of success but rather of value to others.",
                      quotes)
        self.assertEqual(len(quotes), 3)

    def test_strips_refs_templates_links_and_emphasis(self):
        quotes = wikiquote._extract_quotes(SAMPLE_WIKITEXT)
        joined = " ".join(quotes)
        for marker in ("<ref", "[[", "{{", "'''", "Source:"):
            self.assertNotIn(marker, joined)
        self.assertIn("questioning every single day", joined)  # template stripped

    def test_skips_quotes_about_section(self):
        quotes = wikiquote._extract_quotes(SAMPLE_WIKITEXT)
        self.assertFalse(any("someone else praising" in q for q in quotes))

    def test_empty_or_garbage_yields_nothing(self):
        self.assertEqual(wikiquote._extract_quotes(""), [])
        self.assertEqual(wikiquote._extract_quotes("just some prose, no bullets"), [])


class FetchQuotesTest(unittest.TestCase):
    """_fetch_quotes wraps the API call and never raises."""

    def test_returns_parsed_quotes(self):
        with mock.patch.object(wikiquote, "_fetch_wikitext", return_value=SAMPLE_WIKITEXT):
            quotes = wikiquote._fetch_quotes("Someone")
        self.assertEqual(len(quotes), 3)

    def test_network_failure_returns_empty(self):
        with mock.patch.object(wikiquote, "_fetch_wikitext",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(wikiquote._fetch_quotes("Someone"), [])


class CategoryRotationTest(unittest.TestCase):
    """The category pointer cycles so picks never cluster on one figure set."""

    def test_categories_rotate_evenly_across_runs(self):
        cats = ["alpha", "bravo", "charlie", "delta"]
        figures = _fake_figures(cats, per_category=10)
        state: dict = {}
        seen_cats: list[str] = []
        with mock.patch.object(wikiquote, "FIGURES", figures):
            for now in _runs(_dt.datetime(2026, 1, 1, 0, 30), len(cats) * 3):
                _pick_and_commit(state, now)
                seen_cats.append(state[wikiquote.WIKIQUOTE_CATEGORY_KEY])
        for i in range(0, len(seen_cats), len(cats)):
            window = seen_cats[i:i + len(cats)]
            self.assertEqual(sorted(window), sorted(cats),
                             f"picks clustered: {seen_cats}")

    def test_rotation_starts_after_state_category(self):
        figures = _fake_figures(["alpha", "bravo", "charlie"], per_category=5)
        state = {wikiquote.WIKIQUOTE_CATEGORY_KEY: "bravo"}
        with mock.patch.object(wikiquote, "FIGURES", figures):
            _pick_and_commit(state, _dt.datetime(2026, 5, 1, 9))
        self.assertEqual(state[wikiquote.WIKIQUOTE_CATEGORY_KEY], "charlie")

    def test_exhausted_category_is_skipped(self):
        figures = _fake_figures(["alpha", "bravo"], per_category=1)
        now = _dt.datetime(2026, 5, 1, 9)
        state = {
            wikiquote.WIKIQUOTE_SEEN_KEY: {
                "alpha:alpha 0": (now.date() - _dt.timedelta(days=1)).isoformat()
            },
            wikiquote.WIKIQUOTE_CATEGORY_KEY: "bravo",  # rotation would start at alpha
        }
        with mock.patch.object(wikiquote, "FIGURES", figures):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["name"], "bravo 0")


class DeduplicationTest(unittest.TestCase):
    """No figure repeats within the WIKIQUOTE_REPEAT_DAYS window."""

    def test_no_repeats_across_consecutive_runs(self):
        figures = _fake_figures(["a", "b", "c", "d", "e"], per_category=8)
        state: dict = {}
        picked: list[str] = []
        with mock.patch.object(wikiquote, "FIGURES", figures):
            for now in _runs(_dt.datetime(2026, 1, 1, 1, 0), 39):
                before = {wikiquote.WIKIQUOTE_SEEN_KEY:
                          dict(state.get(wikiquote.WIKIQUOTE_SEEN_KEY) or {})}
                _pick_and_commit(state, now)
                picked.append(_pick_id(before, state))
        self.assertEqual(len(picked), len(set(picked)),
                         "a figure repeated inside the no-repeat window")

    def test_figure_eligible_again_after_window(self):
        figures = _fake_figures(["solo"], per_category=1)
        now = _dt.datetime(2026, 6, 1, 12)
        stale = now.date() - _dt.timedelta(days=wikiquote.WIKIQUOTE_REPEAT_DAYS)
        fid = "solo:solo 0"
        state = {wikiquote.WIKIQUOTE_SEEN_KEY: {fid: stale.isoformat()}}
        with mock.patch.object(wikiquote, "FIGURES", figures):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["name"], "solo 0")
        self.assertEqual(state[wikiquote.WIKIQUOTE_SEEN_KEY][fid], now.date().isoformat())

    def test_all_seen_falls_back_to_least_recent(self):
        figures = _fake_figures(["a", "b"], per_category=1)
        now = _dt.datetime(2026, 6, 10, 6)
        state = {
            wikiquote.WIKIQUOTE_SEEN_KEY: {
                "a:a 0": (now.date() - _dt.timedelta(days=5)).isoformat(),  # older
                "b:b 0": (now.date() - _dt.timedelta(days=2)).isoformat(),
            }
        }
        with mock.patch.object(wikiquote, "FIGURES", figures):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["name"], "a 0",
                         "should reuse the least recently shown figure")

    def test_expired_and_malformed_stamps_are_pruned(self):
        figures = _fake_figures(["a"], per_category=3)
        now = _dt.datetime(2026, 6, 10, 15)
        fresh = (now.date() - _dt.timedelta(days=3)).isoformat()
        state = {
            wikiquote.WIKIQUOTE_SEEN_KEY: {
                "a:a 0": (now.date() - _dt.timedelta(days=200)).isoformat(),  # expired
                "a:a 1": "not-a-date",                                        # malformed
                "a:a 2": fresh,                                               # still fresh
            }
        }
        with mock.patch.object(wikiquote, "FIGURES", figures):
            _pick_and_commit(state, now)
        seen = state[wikiquote.WIKIQUOTE_SEEN_KEY]
        picked = [i for i, stamp in seen.items() if stamp == now.date().isoformat()]
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], {"a:a 0", "a:a 1"})  # a 2 still ineligible
        self.assertEqual(seen, {"a:a 2": fresh, picked[0]: now.date().isoformat()})

    def test_same_window_rerun_picks_same_figure(self):
        figures = _fake_figures(["a", "b", "c"], per_category=6)
        base = {
            wikiquote.WIKIQUOTE_SEEN_KEY: {"a:a 1": "2026-06-30"},
            wikiquote.WIKIQUOTE_CATEGORY_KEY: "c",
        }
        with mock.patch.object(wikiquote, "FIGURES", figures):
            first = wikiquote._pick_figure(dict(base), _dt.datetime(2026, 7, 4, 12, 5))
            second = wikiquote._pick_figure(dict(base), _dt.datetime(2026, 7, 4, 13, 59))
        self.assertEqual(first, second)

    def test_consecutive_windows_pick_distinct_figures(self):
        figures = _fake_figures(["a", "b", "c"], per_category=6)
        state: dict = {}
        with mock.patch.object(wikiquote, "FIGURES", figures):
            first = _pick_and_commit(state, _dt.datetime(2026, 7, 4, 12, 5))
            second = _pick_and_commit(state, _dt.datetime(2026, 7, 4, 15, 5))
        self.assertNotEqual(first["id"], second["id"])

    def test_empty_figures_returns_none(self):
        with mock.patch.object(wikiquote, "FIGURES", {}):
            self.assertIsNone(wikiquote._pick_figure({}, _dt.datetime(2026, 1, 1, 0)))


class QuotePickTest(unittest.TestCase):
    """The per-window quote pick is deterministic and skips an empty list."""

    def test_same_window_same_quote(self):
        now = _dt.datetime(2026, 7, 4, 12, 5)
        a = wikiquote._pick_quote(QUOTES, "x:Person", now)
        b = wikiquote._pick_quote(QUOTES, "x:Person", now + _dt.timedelta(minutes=40))
        self.assertEqual(a, b)
        self.assertIn(a, QUOTES)

    def test_empty_quotes_returns_none(self):
        self.assertIsNone(wikiquote._pick_quote([], "x:Person", _dt.datetime(2026, 1, 1)))


class StoryGenerationTest(unittest.TestCase):
    """_generate_story delegates to Gemini (summarize.brief) and clips the result."""

    def test_uses_the_required_prompt_shape(self):
        with mock.patch.object(wikiquote.summarize, "brief", return_value=STORY) as brief:
            wikiquote._generate_story("Albert Einstein", QUOTES[0])
        brief.assert_called_once()
        user_prompt = brief.call_args.args[1]
        self.assertIn("This quote was said by Albert Einstein", user_prompt)
        self.assertIn(QUOTES[0], user_prompt)
        self.assertIn("3 substantial paragraphs", user_prompt)
        self.assertIn("Do not use bullet points", user_prompt)

    def test_none_when_no_provider(self):
        with mock.patch.object(wikiquote.summarize, "brief", return_value=None):
            self.assertIsNone(wikiquote._generate_story("Someone", "A quote here."))

    def test_long_story_is_clipped_to_fit_ntfy(self):
        with mock.patch.object(wikiquote.summarize, "brief",
                               return_value="A sentence here. " * 4000):
            out = wikiquote._generate_story("Someone", "A quote here.")
        self.assertLessEqual(len(out), wikiquote.WIKIQUOTE_CLIP_CHARS)


class ClipStoryTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(wikiquote._clip_story("Just a line."), "Just a line.")

    def test_clips_on_a_sentence_boundary(self):
        text = "First sentence here. Second sentence here. " * 10
        out = wikiquote._clip_story(text, limit=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("."), out)

    def test_falls_back_to_word_boundary_without_sentences(self):
        out = wikiquote._clip_story("word " * 50, limit=20)
        self.assertLessEqual(len(out), 21)  # may add the ellipsis
        self.assertTrue(out.endswith("…"))


class RunTest(unittest.TestCase):
    """The push fires once per 3-hour window with a real quote + Gemini story."""

    def setUp(self):
        self.figures = _fake_figures(["a", "b", "c"], per_category=4)
        for patcher in (
            mock.patch.object(wikiquote, "FIGURES", self.figures),
            mock.patch.object(wikiquote, "_fetch_quotes", return_value=list(QUOTES)),
            mock.patch.object(wikiquote.summarize, "brief", return_value=STORY),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pushes_once_per_window_with_quote_and_story(self):
        now = _dt.datetime(2026, 6, 16, 12, 1)
        with capture_pushes() as sent:
            state = wikiquote._run({}, now)
            wikiquote._run(state, now + _dt.timedelta(minutes=30))  # re-run in window
        self.assertEqual(len(sent), 1, "a re-run inside the window must not resend")
        self.assertRegex(sent[0]["title"], r"^[abc] \d$")  # figure name is the header
        body = sent[0]["message"]
        self.assertTrue(any(q in body for q in QUOTES), "the real quote is in the body")
        self.assertIn(STORY, body)  # the generated narrative follows
        self.assertEqual(state[wikiquote.WIKIQUOTE_SENT_KEY], wikiquote._window(now))

    def test_no_quote_skips_cleanly_and_retries(self):
        now = _dt.datetime(2026, 6, 16, 18, 2)
        with mock.patch.object(wikiquote, "_fetch_quotes", return_value=[]), \
             capture_pushes() as sent:
            state = wikiquote._run({}, now)
        self.assertEqual(sent, [], "no push when no quote could be fetched")
        self.assertNotIn(wikiquote.WIKIQUOTE_SENT_KEY, state)
        self.assertNotIn(wikiquote.WIKIQUOTE_SEEN_KEY, state)

    def test_gemini_failure_skips_cleanly_and_retries(self):
        now = _dt.datetime(2026, 6, 16, 18, 2)
        with mock.patch.object(wikiquote.summarize, "brief", return_value=None), \
             capture_pushes() as sent:
            state = wikiquote._run({}, now)
        self.assertEqual(sent, [], "no push when the story can't be generated")
        self.assertNotIn(wikiquote.WIKIQUOTE_SENT_KEY, state)
        self.assertNotIn(wikiquote.WIKIQUOTE_SEEN_KEY, state)

    def test_pushes_again_next_window_with_distinct_figure(self):
        now = _dt.datetime(2026, 6, 16, 9, 0)
        with capture_pushes() as sent:
            state = wikiquote._run({}, now)
            wikiquote._run(state, now + _dt.timedelta(hours=wikiquote.WIKIQUOTE_WINDOW_HOURS))
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["title"], sent[1]["title"],
                            "consecutive windows must serve distinct figures")

    def test_run_entry_point_fires_without_daily_gate(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = wikiquote.run({})  # the real entry point, real clock
        self.assertEqual(len(sent), 1)
        self.assertIn(wikiquote.WIKIQUOTE_SENT_KEY, state)


class FiguresDataTest(unittest.TestCase):
    """Golden test over the real FIGURES list: a shrunken or malformed list
    fails CI instead of silently weakening the channel."""

    def test_figures_are_well_formed(self):
        figures = wikiquote.FIGURES
        self.assertGreaterEqual(len(figures), 5, "expected several categories")

        all_names: list[str] = []
        for category, names in figures.items():
            self.assertTrue(str(category).strip(), "category id must be non-empty")
            self.assertIsInstance(names, list)
            self.assertGreaterEqual(len(names), 8, f"{category} has only {len(names)}")
            for name in names:
                self.assertTrue(str(name).strip(), f"empty name in {category}")
                all_names.append(name)

        self.assertGreaterEqual(len(all_names), 50, "expected 50+ figures")
        self.assertEqual(len(all_names), len(set(all_names)), "figure names must be unique")

    def test_entries_have_stable_ids(self):
        entries = wikiquote._figure_entries()
        ids = [e["id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "figure ids must be unique")
        for e in entries:
            self.assertEqual(e["id"], f"{e['category']}:{e['name']}")


if __name__ == "__main__":
    unittest.main()
