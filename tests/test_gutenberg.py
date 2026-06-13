"""Tests for the Gemini-narrated Project Gutenberg literary push
(notify_watcher.topics.gutenberg).

On EVERY watcher run (every 3 hours, independent of NOTIFY_DAILY) one work is
chosen from the curated WORKS reading list — genre rotation + a 30-day no-repeat
window, window-seeded for re-run determinism — a REAL passage is fetched from
the book's public-domain plain text (Gutendex, no key), and Gemini
(summarize.brief) narrates a literary/historical context around it. The work is
recorded and the window stamped ONLY after both the passage fetch and the story
succeed, so any failure skips cleanly and retries next run. Gutendex, the text
download, and the LLM are mocked throughout (no network); the golden test
validates the real WORKS list.
"""
from __future__ import annotations

import datetime as _dt
import unittest
from unittest import mock

from notify_watcher.topics import gutenberg
from tests._util import capture_pushes

STORY = ("In a distant age, an author set ideas to paper. The work endured long "
         "after its writer was gone. And so the passage still speaks, read in "
         "wonder by every new generation that finds it.")

# A realistic Gutenberg plain-text file: the license header, the START marker,
# a body with several substantial paragraphs (and some short ones to skip), the
# END marker, then the license footer. The boilerplate must never leak into a
# passage.
_LONG_A = ("It was the season of light and the season of darkness, a time when "
           "every settled certainty seemed to tremble at the edges, and the "
           "people of the city went about their ordinary business as though the "
           "ground beneath them were not quietly shifting toward something new. ")
_LONG_B = ("She walked the length of the long gallery in the failing afternoon, "
           "considering all that had been said and left unsaid between them, and "
           "knew with a sudden and unwelcome clarity that nothing would ever "
           "again be quite so simple as it had once appeared to her young eyes. ")
_LONG_C = ("The old sailor leaned upon the rail and watched the harbour lights "
           "scatter across the black water, each one a small promise of a life "
           "going on somewhere beyond his reach, and he felt the years gather in "
           "his chest like a tide that would not turn back however he might wish. ")
SAMPLE_TEXT = (
    "The Project Gutenberg eBook of A Sample Work\n\n"
    "This ebook is for the use of anyone anywhere at no cost and with almost no "
    "restrictions whatsoever. License boilerplate that must be stripped.\n\n"
    "*** START OF THE PROJECT GUTENBERG EBOOK A SAMPLE WORK ***\n\n"
    "CONTENTS\n\nChapter One\n\n"
    + _LONG_A + "\n\n" + _LONG_B + "\n\n" + "A short line.\n\n" + _LONG_C + "\n\n"
    + _LONG_A + "\n\n" + _LONG_B + "\n\n" + _LONG_C + "\n\n" + _LONG_A + "\n\n"
    "*** END OF THE PROJECT GUTENBERG EBOOK A SAMPLE WORK ***\n\n"
    "Section with the full Project Gutenberg license that must be stripped too.\n"
)


class _FakeResponse:
    def __init__(self, *, json_data=None, text="", status=200):
        self._json = json_data
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _runs(start: _dt.datetime, count: int) -> list[_dt.datetime]:
    """`count` consecutive 3-hour cron firings starting at `start`."""
    step = _dt.timedelta(hours=gutenberg.GUTENBERG_WINDOW_HOURS)
    return [start + step * i for i in range(count)]


def _fake_works(genres: list[str], per_genre: int) -> dict[str, list[dict]]:
    """A synthetic WORKS map with `per_genre` works per genre (stable book ids)."""
    return {
        g: [{"book_id": gi * 1000 + i, "title": f"{g} {i}", "author": f"Author {i}"}
            for i in range(per_genre)]
        for gi, g in enumerate(genres)
    }


def _pick_and_commit(state: dict, now: _dt.datetime) -> dict | None:
    """Select a work and record it — the selection half of run()."""
    chosen = gutenberg._pick_work(state, now)
    if chosen is not None:
        gutenberg._commit(state, chosen, now.date())
    return chosen


def _pick_id(state_before: dict, state_after: dict) -> str:
    """The id newly stamped (or re-stamped today) by a pick+commit."""
    before = dict(state_before.get(gutenberg.GUTENBERG_SEEN_KEY) or {})
    after = state_after[gutenberg.GUTENBERG_SEEN_KEY]
    changed = [i for i, d in after.items() if before.get(i) != d]
    assert len(changed) == 1, f"expected exactly one new stamp, got {changed}"
    return changed[0]


class WindowTest(unittest.TestCase):
    """The 3-hour-window stamp that seeds the pick and guards re-runs."""

    def test_hours_bucket_into_3h_windows(self):
        day = "2026-06-16"
        self.assertEqual(gutenberg._window(_dt.datetime(2026, 6, 16, 0, 0)), f"{day}T0")
        self.assertEqual(gutenberg._window(_dt.datetime(2026, 6, 16, 2, 59)), f"{day}T0")
        self.assertEqual(gutenberg._window(_dt.datetime(2026, 6, 16, 3, 0)), f"{day}T1")
        self.assertEqual(gutenberg._window(_dt.datetime(2026, 6, 16, 23, 59)), f"{day}T7")

    def test_windows_differ_across_days(self):
        self.assertNotEqual(gutenberg._window(_dt.datetime(2026, 6, 16, 1)),
                            gutenberg._window(_dt.datetime(2026, 6, 17, 1)))


class BoilerplateTest(unittest.TestCase):
    """The Gutenberg license header/footer is removed, keeping only the body."""

    def test_strips_header_and_footer(self):
        body = gutenberg._strip_boilerplate(SAMPLE_TEXT)
        self.assertNotIn("License boilerplate", body)
        self.assertNotIn("full Project Gutenberg license", body)
        self.assertIn("season of light", body)

    def test_no_markers_returns_text_unchanged(self):
        self.assertEqual(gutenberg._strip_boilerplate("plain body, no markers"),
                         "plain body, no markers")


class ParagraphTest(unittest.TestCase):
    """Blank-line splitting folds Gutenberg's hard wraps into flowing prose."""

    def test_folds_hard_wraps_and_splits_on_blank_lines(self):
        paras = gutenberg._paragraphs("line one\nstill one\n\npara two here")
        self.assertEqual(paras, ["line one still one", "para two here"])

    def test_empty_body_yields_no_paragraphs(self):
        self.assertEqual(gutenberg._paragraphs("   \n\n   "), [])

    def test_strips_publisher_editorial_inserts(self):
        body = ("She spoke at last. [Illustration: A drawing "
                "[_Copyright 1894 by George Allen._]] And then she left.")
        (para,) = gutenberg._paragraphs(body)
        for marker in ("Illustration", "Copyright", "[", "]"):
            self.assertNotIn(marker, para)
        self.assertIn("She spoke at last.", para)
        self.assertIn("And then she left.", para)


class ExtractPassageTest(unittest.TestCase):
    """A real, sane-length body passage is extracted, never boilerplate."""

    def test_passage_is_in_band_and_from_the_body(self):
        passage = gutenberg._extract_passage(SAMPLE_TEXT, "seed-1")
        self.assertIsNotNone(passage)
        self.assertGreaterEqual(len(passage), gutenberg._PASSAGE_MIN_CHARS)
        self.assertLessEqual(len(passage), gutenberg._PASSAGE_MAX_CHARS)
        for marker in ("Project Gutenberg", "License", "CONTENTS"):
            self.assertNotIn(marker, passage)

    def test_same_seed_same_passage(self):
        a = gutenberg._extract_passage(SAMPLE_TEXT, "seed-x")
        b = gutenberg._extract_passage(SAMPLE_TEXT, "seed-x")
        self.assertEqual(a, b)

    def test_body_without_substantial_prose_returns_none(self):
        thin = ("*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n"
                "short\n\ntiny\n\nbrief\n\n"
                "*** END OF THE PROJECT GUTENBERG EBOOK X ***")
        self.assertIsNone(gutenberg._extract_passage(thin, "seed"))


class PlainTextUrlTest(unittest.TestCase):
    """Gutendex format selection prefers UTF-8 and rejects zip archives."""

    def _patch_get(self, formats):
        return mock.patch.object(
            gutenberg.requests, "get",
            return_value=_FakeResponse(json_data={"formats": formats}))

    def test_prefers_utf8_plain_text(self):
        with self._patch_get({
            "text/plain; charset=us-ascii": "http://x/ascii.txt",
            "text/plain; charset=utf-8": "http://x/utf8.txt",
            "application/epub+zip": "http://x/book.epub",
        }):
            self.assertEqual(gutenberg._plain_text_url(123), "http://x/utf8.txt")

    def test_falls_back_to_any_plain_text(self):
        with self._patch_get({"text/plain": "http://x/plain.txt"}):
            self.assertEqual(gutenberg._plain_text_url(123), "http://x/plain.txt")

    def test_skips_zipped_plain_text(self):
        with self._patch_get({"text/plain; charset=utf-8": "http://x/book.txt.zip"}):
            self.assertIsNone(gutenberg._plain_text_url(123))

    def test_no_plain_text_returns_none(self):
        with self._patch_get({"application/epub+zip": "http://x/book.epub"}):
            self.assertIsNone(gutenberg._plain_text_url(123))


class FetchPassageTest(unittest.TestCase):
    """_fetch_passage wraps the network calls and never raises."""

    def test_returns_passage_on_success(self):
        with mock.patch.object(gutenberg, "_plain_text_url", return_value="http://x/t.txt"), \
             mock.patch.object(gutenberg, "_fetch_text", return_value=SAMPLE_TEXT):
            passage = gutenberg._fetch_passage(42, "seed")
        self.assertIsNotNone(passage)
        self.assertGreaterEqual(len(passage), gutenberg._PASSAGE_MIN_CHARS)

    def test_no_plain_text_url_returns_none(self):
        with mock.patch.object(gutenberg, "_plain_text_url", return_value=None):
            self.assertIsNone(gutenberg._fetch_passage(42, "seed"))

    def test_metadata_failure_returns_none(self):
        with mock.patch.object(gutenberg, "_plain_text_url",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(gutenberg._fetch_passage(42, "seed"))

    def test_text_download_failure_returns_none(self):
        with mock.patch.object(gutenberg, "_plain_text_url", return_value="http://x/t.txt"), \
             mock.patch.object(gutenberg, "_fetch_text", side_effect=RuntimeError("boom")):
            self.assertIsNone(gutenberg._fetch_passage(42, "seed"))


class GenreRotationTest(unittest.TestCase):
    """The genre pointer cycles so picks never cluster on one shelf."""

    def test_genres_rotate_evenly_across_runs(self):
        genres = ["alpha", "bravo", "charlie", "delta"]
        works = _fake_works(genres, per_genre=10)
        state: dict = {}
        seen: list[str] = []
        with mock.patch.object(gutenberg, "WORKS", works):
            for now in _runs(_dt.datetime(2026, 1, 1, 0, 30), len(genres) * 3):
                _pick_and_commit(state, now)
                seen.append(state[gutenberg.GUTENBERG_GENRE_KEY])
        for i in range(0, len(seen), len(genres)):
            window = seen[i:i + len(genres)]
            self.assertEqual(sorted(window), sorted(genres), f"picks clustered: {seen}")

    def test_rotation_starts_after_state_genre(self):
        works = _fake_works(["alpha", "bravo", "charlie"], per_genre=5)
        state = {gutenberg.GUTENBERG_GENRE_KEY: "bravo"}
        with mock.patch.object(gutenberg, "WORKS", works):
            _pick_and_commit(state, _dt.datetime(2026, 5, 1, 9))
        self.assertEqual(state[gutenberg.GUTENBERG_GENRE_KEY], "charlie")

    def test_exhausted_genre_is_skipped(self):
        works = _fake_works(["alpha", "bravo"], per_genre=1)
        now = _dt.datetime(2026, 5, 1, 9)
        state = {
            gutenberg.GUTENBERG_SEEN_KEY: {
                "alpha:0": (now.date() - _dt.timedelta(days=1)).isoformat()
            },
            gutenberg.GUTENBERG_GENRE_KEY: "bravo",  # rotation would start at alpha
        }
        with mock.patch.object(gutenberg, "WORKS", works):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["genre"], "bravo")


class DeduplicationTest(unittest.TestCase):
    """No work repeats within the GUTENBERG_REPEAT_DAYS window."""

    def test_no_repeats_across_consecutive_runs(self):
        works = _fake_works(["a", "b", "c", "d", "e"], per_genre=8)
        state: dict = {}
        picked: list[str] = []
        with mock.patch.object(gutenberg, "WORKS", works):
            for now in _runs(_dt.datetime(2026, 1, 1, 1, 0), 39):
                before = {gutenberg.GUTENBERG_SEEN_KEY:
                          dict(state.get(gutenberg.GUTENBERG_SEEN_KEY) or {})}
                _pick_and_commit(state, now)
                picked.append(_pick_id(before, state))
        self.assertEqual(len(picked), len(set(picked)),
                         "a work repeated inside the no-repeat window")

    def test_work_eligible_again_after_window(self):
        works = _fake_works(["solo"], per_genre=1)
        now = _dt.datetime(2026, 6, 1, 12)
        stale = now.date() - _dt.timedelta(days=gutenberg.GUTENBERG_REPEAT_DAYS)
        wid = "solo:0"
        state = {gutenberg.GUTENBERG_SEEN_KEY: {wid: stale.isoformat()}}
        with mock.patch.object(gutenberg, "WORKS", works):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["id"], wid)
        self.assertEqual(state[gutenberg.GUTENBERG_SEEN_KEY][wid], now.date().isoformat())

    def test_all_seen_falls_back_to_least_recent(self):
        works = _fake_works(["a", "b"], per_genre=1)
        now = _dt.datetime(2026, 6, 10, 6)
        state = {
            gutenberg.GUTENBERG_SEEN_KEY: {
                "a:0": (now.date() - _dt.timedelta(days=5)).isoformat(),    # older
                "b:1000": (now.date() - _dt.timedelta(days=2)).isoformat(),
            }
        }
        with mock.patch.object(gutenberg, "WORKS", works):
            chosen = _pick_and_commit(state, now)
        self.assertEqual(chosen["id"], "a:0",
                         "should reuse the least recently shown work")

    def test_expired_and_malformed_stamps_are_pruned(self):
        works = _fake_works(["a"], per_genre=3)
        now = _dt.datetime(2026, 6, 10, 15)
        fresh = (now.date() - _dt.timedelta(days=3)).isoformat()
        state = {
            gutenberg.GUTENBERG_SEEN_KEY: {
                "a:0": (now.date() - _dt.timedelta(days=200)).isoformat(),  # expired
                "a:1": "not-a-date",                                        # malformed
                "a:2": fresh,                                               # still fresh
            }
        }
        with mock.patch.object(gutenberg, "WORKS", works):
            _pick_and_commit(state, now)
        seen = state[gutenberg.GUTENBERG_SEEN_KEY]
        picked = [i for i, stamp in seen.items() if stamp == now.date().isoformat()]
        self.assertEqual(len(picked), 1)
        self.assertIn(picked[0], {"a:0", "a:1"})  # a:2 still ineligible
        self.assertEqual(seen, {"a:2": fresh, picked[0]: now.date().isoformat()})

    def test_same_window_rerun_picks_same_work(self):
        works = _fake_works(["a", "b", "c"], per_genre=6)
        base = {
            gutenberg.GUTENBERG_SEEN_KEY: {"a:1": "2026-06-30"},
            gutenberg.GUTENBERG_GENRE_KEY: "c",
        }
        with mock.patch.object(gutenberg, "WORKS", works):
            first = gutenberg._pick_work(dict(base), _dt.datetime(2026, 7, 4, 12, 5))
            second = gutenberg._pick_work(dict(base), _dt.datetime(2026, 7, 4, 13, 59))
        self.assertEqual(first, second)

    def test_consecutive_windows_pick_distinct_works(self):
        works = _fake_works(["a", "b", "c"], per_genre=6)
        state: dict = {}
        with mock.patch.object(gutenberg, "WORKS", works):
            first = _pick_and_commit(state, _dt.datetime(2026, 7, 4, 12, 5))
            second = _pick_and_commit(state, _dt.datetime(2026, 7, 4, 15, 5))
        self.assertNotEqual(first["id"], second["id"])

    def test_empty_works_returns_none(self):
        with mock.patch.object(gutenberg, "WORKS", {}):
            self.assertIsNone(gutenberg._pick_work({}, _dt.datetime(2026, 1, 1, 0)))


class StoryGenerationTest(unittest.TestCase):
    """_generate_story delegates to Gemini (summarize.brief) and clips the result."""

    def test_uses_the_required_prompt_shape(self):
        passage = "To be, or not to be, that is the question."
        with mock.patch.object(gutenberg.summarize, "brief", return_value=STORY) as brief:
            gutenberg._generate_story("Hamlet", "William Shakespeare", passage)
        brief.assert_called_once()
        user_prompt = brief.call_args.args[1]
        self.assertIn("This passage is from Hamlet by William Shakespeare", user_prompt)
        self.assertIn(passage, user_prompt)
        self.assertIn("at least 3 substantial paragraphs", user_prompt)
        self.assertIn("Do not use bullet points", user_prompt)
        self.assertIn("why it still resonates today", user_prompt)

    def test_none_when_no_provider(self):
        with mock.patch.object(gutenberg.summarize, "brief", return_value=None):
            self.assertIsNone(gutenberg._generate_story("W", "A", "a passage here"))

    def test_long_story_is_clipped_to_fit_ntfy(self):
        with mock.patch.object(gutenberg.summarize, "brief",
                               return_value="A sentence here. " * 4000):
            out = gutenberg._generate_story("W", "A", "a passage here")
        self.assertLessEqual(len(out), gutenberg.GUTENBERG_CLIP_CHARS)


class ClipStoryTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(gutenberg._clip_story("Just a line."), "Just a line.")

    def test_clips_on_a_sentence_boundary(self):
        text = "First sentence here. Second sentence here. " * 10
        out = gutenberg._clip_story(text, limit=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("."), out)

    def test_falls_back_to_word_boundary_without_sentences(self):
        out = gutenberg._clip_story("word " * 50, limit=20)
        self.assertLessEqual(len(out), 21)  # may add the ellipsis
        self.assertTrue(out.endswith("…"))


class RunTest(unittest.TestCase):
    """The push fires once per 3-hour window with a real passage + Gemini story."""

    PASSAGE = ("It was the best of times, it was the worst of times, it was the "
               "age of wisdom, it was the age of foolishness, and it was a season "
               "that asked everything of those who lived through it.")

    def setUp(self):
        self.works = _fake_works(["a", "b", "c"], per_genre=4)
        for patcher in (
            mock.patch.object(gutenberg, "WORKS", self.works),
            mock.patch.object(gutenberg, "_fetch_passage", return_value=self.PASSAGE),
            mock.patch.object(gutenberg.summarize, "brief", return_value=STORY),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pushes_once_per_window_with_passage_and_story(self):
        now = _dt.datetime(2026, 6, 16, 12, 1)
        with capture_pushes() as sent:
            state = gutenberg._run({}, now)
            gutenberg._run(state, now + _dt.timedelta(minutes=30))  # re-run in window
        self.assertEqual(len(sent), 1, "a re-run inside the window must not resend")
        self.assertRegex(sent[0]["title"], r"^[abc] \d — Author \d$")
        body = sent[0]["message"]
        self.assertIn(self.PASSAGE, body, "the real passage is in the body")
        self.assertIn(STORY, body)  # the generated narrative follows
        self.assertEqual(state[gutenberg.GUTENBERG_SENT_KEY], gutenberg._window(now))

    def test_no_passage_skips_cleanly_and_retries(self):
        now = _dt.datetime(2026, 6, 16, 18, 2)
        with mock.patch.object(gutenberg, "_fetch_passage", return_value=None), \
             capture_pushes() as sent:
            state = gutenberg._run({}, now)
        self.assertEqual(sent, [], "no push when no passage could be fetched")
        self.assertNotIn(gutenberg.GUTENBERG_SENT_KEY, state)
        self.assertNotIn(gutenberg.GUTENBERG_SEEN_KEY, state)

    def test_gemini_failure_skips_cleanly_and_retries(self):
        now = _dt.datetime(2026, 6, 16, 18, 2)
        with mock.patch.object(gutenberg.summarize, "brief", return_value=None), \
             capture_pushes() as sent:
            state = gutenberg._run({}, now)
        self.assertEqual(sent, [], "no push when the story can't be generated")
        self.assertNotIn(gutenberg.GUTENBERG_SENT_KEY, state)
        self.assertNotIn(gutenberg.GUTENBERG_SEEN_KEY, state)

    def test_pushes_again_next_window_with_distinct_work(self):
        now = _dt.datetime(2026, 6, 16, 9, 0)
        with capture_pushes() as sent:
            state = gutenberg._run({}, now)
            gutenberg._run(state, now + _dt.timedelta(hours=gutenberg.GUTENBERG_WINDOW_HOURS))
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["title"], sent[1]["title"],
                            "consecutive windows must serve distinct works")

    def test_run_entry_point_fires_without_daily_gate(self):
        with mock.patch.dict("os.environ", {"NOTIFY_DAILY": ""}, clear=False), \
             capture_pushes() as sent:
            state = gutenberg.run({})  # the real entry point, real clock
        self.assertEqual(len(sent), 1)
        self.assertIn(gutenberg.GUTENBERG_SENT_KEY, state)


class WorksDataTest(unittest.TestCase):
    """Golden test over the real WORKS list: a shrunken or malformed list fails
    CI instead of silently weakening the channel."""

    def test_works_are_well_formed(self):
        works = gutenberg.WORKS
        self.assertGreaterEqual(len(works), 5, "expected several genres")

        all_ids: list[int] = []
        for genre, entries in works.items():
            self.assertTrue(str(genre).strip(), "genre id must be non-empty")
            self.assertIsInstance(entries, list)
            self.assertGreaterEqual(len(entries), 8, f"{genre} has only {len(entries)}")
            for w in entries:
                self.assertIsInstance(w.get("book_id"), int, f"bad book_id in {genre}")
                self.assertTrue(str(w.get("title", "")).strip(), f"empty title in {genre}")
                self.assertTrue(str(w.get("author", "")).strip(), f"empty author in {genre}")
                all_ids.append(w["book_id"])

        self.assertGreaterEqual(len(all_ids), 50, "expected 50+ works")
        self.assertEqual(len(all_ids), len(set(all_ids)), "Gutenberg book ids must be unique")

    def test_entries_have_stable_ids(self):
        entries = gutenberg._work_entries()
        ids = [e["id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "work ids must be unique")
        for e in entries:
            self.assertEqual(e["id"], f"{e['genre']}:{e['book_id']}")


if __name__ == "__main__":
    unittest.main()
