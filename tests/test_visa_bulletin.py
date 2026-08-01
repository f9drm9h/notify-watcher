"""Tests for the Visa Bulletin parser (notify_watcher.topics.visa_bulletin).

SOURCE CHANGE, August 2026: travel.state.gov's HTML moved behind Cloudflare bot
management (HTTP 403, `cf-mitigated: challenge`), so the topic now reads the
State Department's own PDF under /content/dam/, which is served plainly. These
tests pin the PDF parser against the real flattened-text shape and keep every
edition-tracking behavior the HTML version had.

The fixture below is the genuine pypdf output shape captured from the August
2026 bulletin — column headers split across lines, then one preference row per
line. That layout is exactly what silently breaks, and it is the reason
_f4_all_other raises rather than guesses: these are immigration dates, and a
confidently-wrong number is far worse than a loud failure.
"""
from __future__ import annotations

import datetime as _dt
import os
import unittest
from unittest import mock

from notify_watcher.topics import visa_bulletin as vb
from tests._util import capture_pushes

# Real shape: pypdf flattens the two family-sponsored tables like this. The
# employment-based tables use E1-E5, so F4 appears exactly twice in a bulletin.
BULLETIN_PDF_TEXT = """
VISA BULLETIN FOR AUGUST 2026
Number 98
Volume X

A.  FINAL ACTION DATES FOR FAMILY-SPONSORED PREFERENCE CASES
Family-
Sponsored
All Chargeability
Areas Except
Those Listed
CHINA-
mainland
born
INDIA MEXICO PHILIPPINES
F1 15DEC18 15DEC18 15DEC18 01DEC07 01MAY13
F2A 22JUL26 22JUL26 22JUL26 22JUL25 22JUL26
F2B 01JAN18 01JAN18 01JAN18 15FEB09 01JUN13
F3 15MAY12 15MAY12 15MAY12 01JUL01 22FEB06
F4 08NOV08 08NOV08 01NOV06 08APR01 01AUG07

B.  DATES FOR FILING FAMILY-SPONSORED VISA APPLICATIONS
Family-
Sponsored
All Chargeability
Areas Except
Those Listed
CHINA-
mainland
born
INDIA MEXICO PHILIPPINES
F1 15JUN19 15JUN19 15JUN19 01DEC08 22APR15
F2A C C C C C
F4 22DEC09 22DEC09 15DEC06 30APR01 22MAR08

C.  FINAL ACTION DATES FOR EMPLOYMENT-BASED PREFERENCE CASES
Employment-
based
1st C C 15FEB22 C C
"""

# Same bulletin with the Final Action F4 cell advanced one week; Dates for
# Filing held steady.
BULLETIN_PDF_ADVANCED = BULLETIN_PDF_TEXT.replace(
    "F4 08NOV08 08NOV08", "F4 15NOV08 08NOV08", 1)

# A state that already tracks the July edition with both cells current.
_TRACKING_JULY = {
    vb.EDITION_KEY: "2026-07",
    "visa_f4_final_action": "08NOV08",
    "visa_f4_dates_for_filing": "22DEC09",
}


class F4ParserTest(unittest.TestCase):
    def test_reads_both_f4_all_other_cells(self):
        cells = vb._f4_all_other(BULLETIN_PDF_TEXT)
        self.assertEqual(cells["final_action"], "08NOV08")
        self.assertEqual(cells["dates_for_filing"], "22DEC09")

    def test_employment_tables_do_not_confuse_the_parser(self):
        # E1-E5 rows must never be mistaken for an F4 row, even though the
        # employment section repeats the "FINAL ACTION DATES" heading.
        cells = vb._f4_all_other(BULLETIN_PDF_TEXT)
        self.assertEqual(len(cells), 2)

    def test_section_order_is_not_assumed(self):
        # Each F4 row is attributed to its NEAREST PRECEDING heading, so a
        # bulletin that ever printed Dates-for-Filing first still parses right.
        head, _, tail = BULLETIN_PDF_TEXT.partition("B.  DATES FOR FILING")
        swapped = "B.  DATES FOR FILING" + tail.split("C.  FINAL ACTION")[0] + head
        cells = vb._f4_all_other(swapped)
        self.assertEqual(cells["final_action"], "08NOV08")
        self.assertEqual(cells["dates_for_filing"], "22DEC09")

    def test_missing_f4_row_raises_rather_than_guessing(self):
        text = BULLETIN_PDF_TEXT.replace("F4 22DEC09 22DEC09", "F5 22DEC09 22DEC09")
        with self.assertRaises(RuntimeError) as ctx:
            vb._f4_all_other(text)
        self.assertIn("dates_for_filing", str(ctx.exception))

    def test_empty_document_raises(self):
        with self.assertRaises(RuntimeError):
            vb._f4_all_other("")

    def test_current_and_unavailable_cells_pass_through(self):
        text = BULLETIN_PDF_TEXT.replace("F4 08NOV08 08NOV08", "F4 C C", 1)
        self.assertEqual(vb._f4_all_other(text)["final_action"], "C")


class NormTest(unittest.TestCase):
    def test_collapses_nbsp_and_runs(self):
        self.assertEqual(vb._norm("A.  FINAL   ACTION"), "A. FINAL ACTION")


class UrlHelpersTest(unittest.TestCase):
    def test_pdf_url_matches_the_published_naming(self):
        self.assertEqual(
            vb._pdf_url(2026, 8),
            "https://travel.state.gov/content/dam/visas/Bulletins/"
            "visabulletin_August2026.pdf")

    def test_html_url_is_the_human_click_target(self):
        self.assertEqual(
            vb._html_url(2026, 8),
            "https://travel.state.gov/content/travel/en/legal/visa-law0/"
            "visa-bulletin/2026/visa-bulletin-for-august-2026.html")

    def test_shift_month_crosses_year_boundaries(self):
        self.assertEqual(vb._shift_month(2026, 12, 1), (2027, 1))
        self.assertEqual(vb._shift_month(2026, 1, -1), (2025, 12))
        self.assertEqual(vb._shift_month(2026, 7, 2), (2026, 9))


class FindCurrentBulletinTest(unittest.TestCase):
    """Discovery walks DOWN from a couple of months ahead, taking the first
    edition that actually exists — so an early publication is picked up at once
    and a late one simply resolves to the previous edition."""

    def _with_published(self, published: set[str]):
        return mock.patch.object(vb, "_pdf_exists",
                                 side_effect=lambda url: any(p in url for p in published))

    def test_picks_the_newest_published_edition(self):
        with self._with_published({"July2026", "August2026"}):
            edition, url = vb._find_current_bulletin(_dt.date(2026, 7, 20))
        self.assertEqual(edition, "2026-08")
        self.assertIn("visabulletin_August2026.pdf", url)

    def test_falls_back_to_the_previous_edition_when_the_next_is_late(self):
        with self._with_published({"July2026"}):
            edition, _ = vb._find_current_bulletin(_dt.date(2026, 7, 20))
        self.assertEqual(edition, "2026-07")

    def test_finds_an_early_published_edition_ahead_of_the_calendar(self):
        with self._with_published({"September2026"}):
            edition, _ = vb._find_current_bulletin(_dt.date(2026, 7, 20))
        self.assertEqual(edition, "2026-09")

    def test_nothing_published_raises_loudly(self):
        with self._with_published(set()):
            with self.assertRaises(RuntimeError) as ctx:
                vb._find_current_bulletin(_dt.date(2026, 7, 20))
        self.assertIn("changed", str(ctx.exception))

    def test_head_transport_error_is_not_treated_as_absence(self):
        # A network blip must not be read as "no bulletin published"; it just
        # makes that one probe fail so discovery keeps walking back.
        import requests
        with mock.patch.object(vb.requests, "head",
                               side_effect=requests.RequestException("boom")):
            self.assertFalse(vb._pdf_exists(vb._pdf_url(2026, 8)))

    def test_non_pdf_response_is_not_accepted(self):
        # A 200 that serves an HTML error page must not count as published.
        resp = mock.Mock(status_code=200, headers={"content-type": "text/html"})
        with mock.patch.object(vb.requests, "head", return_value=resp):
            self.assertFalse(vb._pdf_exists(vb._pdf_url(2026, 8)))


class EditionHelpersTest(unittest.TestCase):
    def test_expected_edition_is_next_month(self):
        self.assertEqual(vb._expected_edition(_dt.date(2026, 7, 16)), "2026-08")

    def test_expected_edition_december_rolls_to_january(self):
        self.assertEqual(vb._expected_edition(_dt.date(2026, 12, 20)), "2027-01")

    def test_edition_label(self):
        self.assertEqual(vb._edition_label("2026-08"), "August 2026")


class RunEditionTrackingTest(unittest.TestCase):
    """run()-level behavior of the edition tracker and the late-bulletin alert.

    Discovery and the PDF download are stubbed; capture_pushes records what
    actually went out. NOTIFY_DAILY is cleared so the quarterly check-in stays
    out of the way. Every assertion here predates the source change — the point
    is that swapping HTML for PDF changed nothing a user can observe.
    """

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"NOTIFY_DAILY": ""})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run(self, state, edition, pdf_text, today):
        year, month = int(edition[:4]), int(edition[5:])
        with mock.patch.object(vb, "_find_current_bulletin",
                               return_value=(edition, vb._pdf_url(year, month))), \
                mock.patch.object(vb, "_fetch_pdf_text", return_value=pdf_text), \
                capture_pushes() as sent:
            state = vb.run(state, today=today)
        return state, sent

    def test_first_run_seeds_edition_silently(self):
        state, sent = self._run({}, "2026-07", BULLETIN_PDF_TEXT,
                                _dt.date(2026, 7, 5))
        self.assertEqual(state[vb.EDITION_KEY], "2026-07")
        titles = [p["title"] for p in sent]
        self.assertFalse(any(t.startswith("New visa bulletin") for t in titles))
        self.assertNotIn("Visa bulletin is late", titles)
        # The existing first-seen cell pushes still fire on day one.
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("First seen" in p["message"] for p in sent))

    def test_new_bulletin_with_date_change_is_one_combined_push(self):
        state, sent = self._run(dict(_TRACKING_JULY), "2026-08",
                                BULLETIN_PDF_ADVANCED, _dt.date(2026, 7, 12))
        self.assertEqual(state[vb.EDITION_KEY], "2026-08")
        self.assertEqual(state["visa_f4_final_action"], "15NOV08")
        self.assertEqual(len(sent), 1)
        push = sent[0]
        self.assertEqual(push["title"], "New visa bulletin: August 2026")
        self.assertEqual(push["severity"], "critical")
        # One body carries both cells: the Final Action move and the held line.
        self.assertIn("Final Action", push["message"])
        self.assertIn("held steady at 22DEC09", push["message"])
        # The move still feeds the wait-estimator history.
        self.assertEqual(len(state.get("f4_history") or []), 1)

    def test_new_bulletin_without_date_change_still_notifies(self):
        state, sent = self._run(dict(_TRACKING_JULY), "2026-08",
                                BULLETIN_PDF_TEXT, _dt.date(2026, 7, 12))
        self.assertEqual(state[vb.EDITION_KEY], "2026-08")
        self.assertEqual(len(sent), 1)
        push = sent[0]
        self.assertEqual(push["title"], "New visa bulletin: August 2026")
        self.assertEqual(push["severity"], "moderate")
        self.assertIn("held steady at 08NOV08", push["message"])
        self.assertIn("held steady at 22DEC09", push["message"])

    def test_click_url_points_at_the_readable_html_page(self):
        # The PDF is what we parse; a human tapping the embed should land on the
        # page, which their real browser clears the challenge for.
        state, sent = self._run(dict(_TRACKING_JULY), "2026-08",
                                BULLETIN_PDF_TEXT, _dt.date(2026, 7, 12))
        self.assertIn("visa-bulletin-for-august-2026.html", sent[0]["click_url"])

    def test_no_new_bulletin_before_the_15th_is_silent(self):
        state, sent = self._run(dict(_TRACKING_JULY), "2026-07",
                                BULLETIN_PDF_TEXT, _dt.date(2026, 7, 10))
        self.assertEqual(sent, [])
        self.assertEqual(state[vb.EDITION_KEY], "2026-07")
        self.assertNotIn(vb.LATE_KEY, state)

    def test_late_alert_fires_once_then_rearms_on_new_bulletin(self):
        # July 16: still no August bulletin -> exactly one late alert.
        state, sent = self._run(dict(_TRACKING_JULY), "2026-07",
                                BULLETIN_PDF_TEXT, _dt.date(2026, 7, 16))
        self.assertEqual([p["title"] for p in sent], ["Visa bulletin is late"])
        self.assertEqual(state[vb.LATE_KEY], "2026-08")

        # July 17: still late, but the alert must not repeat.
        state, sent = self._run(state, "2026-07", BULLETIN_PDF_TEXT,
                                _dt.date(2026, 7, 17))
        self.assertEqual(sent, [])

        # July 20: the August bulletin lands -> new-bulletin push, no late alert.
        state, sent = self._run(state, "2026-08", BULLETIN_PDF_TEXT,
                                _dt.date(2026, 7, 20))
        self.assertEqual([p["title"] for p in sent],
                         ["New visa bulletin: August 2026"])


if __name__ == "__main__":
    unittest.main()
