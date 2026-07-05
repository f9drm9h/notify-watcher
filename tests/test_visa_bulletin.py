"""Tests for the Visa Bulletin parser (notify_watcher.topics.visa_bulletin).

Pins _table_after_heading + _f4_all_other against a captured-shape bulletin page
(two family-sponsored tables: Final Action Dates and Dates for Filing, each with
the F4 "All Chargeability Areas Except Those Listed" cell). travel.state.gov
quietly restructures these tables; without this test such a change would only
show up as a "F4 row not found" log line and a silently missed alert.
"""
from __future__ import annotations

import datetime as _dt
import os
import unittest
from unittest import mock

from bs4 import BeautifulSoup

from notify_watcher.topics import visa_bulletin as vb
from tests._util import capture_pushes

# Captured-shape bulletin: the real page wraps each table's heading in a <p> with
# non-breaking spaces, then renders the preference table immediately after. Both
# family-sponsored sections appear; the F4 "All Other" cell is the 2nd column.
BULLETIN_HTML = """
<html><body>
  <p>A. FINAL ACTION DATES FOR FAMILY-SPONSORED PREFERENCE CASES</p>
  <table>
    <tr><th>Family-Sponsored</th><th>All Chargeability Areas Except Those Listed</th>
        <th>CHINA-mainland born</th><th>INDIA</th><th>MEXICO</th><th>PHILIPPINES</th></tr>
    <tr><td>F1</td><td>01SEP15</td><td>01SEP15</td><td>01SEP15</td><td>22APR05</td><td>01MAR12</td></tr>
    <tr><td>F2A</td><td>C</td><td>C</td><td>C</td><td>C</td><td>C</td></tr>
    <tr><td>F4</td><td>08NOV08</td><td>08NOV08</td><td>15AUG06</td><td>15MAR01</td><td>08JAN05</td></tr>
  </table>

  <p>B. DATES FOR FILING FAMILY-SPONSORED VISA APPLICATIONS</p>
  <table>
    <tr><th>Family-Sponsored</th><th>All Chargeability Areas Except Those Listed</th>
        <th>CHINA-mainland born</th><th>INDIA</th><th>MEXICO</th><th>PHILIPPINES</th></tr>
    <tr><td>F1</td><td>01SEP17</td><td>01SEP17</td><td>01SEP17</td><td>01MAY06</td><td>22APR15</td></tr>
    <tr><td>F4</td><td>22DEC09</td><td>22DEC09</td><td>01OCT07</td><td>30APR04</td><td>01OCT06</td></tr>
  </table>
</body></html>
"""

# Employment-based-only page: the family-sponsored heading/table is absent.
NO_FAMILY_HTML = """
<html><body>
  <p>A. FINAL ACTION DATES FOR EMPLOYMENT-BASED PREFERENCE CASES</p>
  <table><tr><td>1st</td><td>C</td></tr></table>
</body></html>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TableAfterHeadingTest(unittest.TestCase):
    def test_finds_final_action_table(self):
        table = vb._table_after_heading(_soup(BULLETIN_HTML),
                                        ("FINAL ACTION DATES", "FAMILY-SPONSORED"))
        self.assertIsNotNone(table)
        self.assertEqual(vb._f4_all_other(table), "08NOV08")

    def test_finds_dates_for_filing_table(self):
        table = vb._table_after_heading(_soup(BULLETIN_HTML),
                                        ("DATES FOR FILING", "FAMILY-SPONSORED"))
        self.assertIsNotNone(table)
        self.assertEqual(vb._f4_all_other(table), "22DEC09")

    def test_missing_heading_returns_none(self):
        self.assertIsNone(
            vb._table_after_heading(_soup(NO_FAMILY_HTML),
                                    ("FINAL ACTION DATES", "FAMILY-SPONSORED"))
        )


class F4AllOtherTest(unittest.TestCase):
    def test_reads_second_column_of_f4_row(self):
        soup = _soup(BULLETIN_HTML)
        table = vb._table_after_heading(soup, ("FINAL ACTION DATES", "FAMILY-SPONSORED"))
        self.assertEqual(vb._f4_all_other(table), "08NOV08")

    def test_missing_f4_row_raises(self):
        soup = _soup("<table><tr><td>F1</td><td>01SEP15</td></tr></table>")
        with self.assertRaises(RuntimeError):
            vb._f4_all_other(soup.find("table"))


class NormTest(unittest.TestCase):
    def test_collapses_nbsp_and_runs(self):
        self.assertEqual(vb._norm("A.  FINAL   ACTION"), "A. FINAL ACTION")


# Index pages: the newest monthly link decides both the bulletin fetched and
# the edition identity ("2026-07"). The August variant is what the index looks
# like after the next edition publishes mid-July.
INDEX_HTML_JULY = """
<html><body>
  <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-june-2026.html">June 2026</a>
  <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-july-2026.html">July 2026</a>
</body></html>
"""
INDEX_HTML_AUGUST = """
<html><body>
  <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-july-2026.html">July 2026</a>
  <a href="/content/travel/en/legal/visa-law0/visa-bulletin/2026/visa-bulletin-for-august-2026.html">August 2026</a>
</body></html>
"""

# Same shape as BULLETIN_HTML but the Final Action F4 cell advanced one week;
# Dates for Filing held steady.
BULLETIN_HTML_ADVANCED = BULLETIN_HTML.replace(
    "<tr><td>F4</td><td>08NOV08</td><td>08NOV08</td>",
    "<tr><td>F4</td><td>15NOV08</td><td>08NOV08</td>", 1)

# A state that already tracks the July edition with both cells current.
_TRACKING_JULY = {
    vb.EDITION_KEY: "2026-07",
    "visa_f4_final_action": "08NOV08",
    "visa_f4_dates_for_filing": "22DEC09",
}


class EditionHelpersTest(unittest.TestCase):
    def test_expected_edition_is_next_month(self):
        self.assertEqual(vb._expected_edition(_dt.date(2026, 7, 16)), "2026-08")

    def test_expected_edition_december_rolls_to_january(self):
        self.assertEqual(vb._expected_edition(_dt.date(2026, 12, 20)), "2027-01")

    def test_edition_label(self):
        self.assertEqual(vb._edition_label("2026-08"), "August 2026")


class RunEditionTrackingTest(unittest.TestCase):
    """run()-level behavior of the edition tracker and the late-bulletin alert.

    _fetch is stubbed with [index page, bulletin page]; capture_pushes records
    what actually went out. NOTIFY_DAILY is cleared so the quarterly check-in
    stays out of the way.
    """

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"NOTIFY_DAILY": ""})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run(self, state, index_html, bulletin_html, today):
        with mock.patch.object(vb, "_fetch",
                               side_effect=[index_html, bulletin_html]), \
                capture_pushes() as sent:
            state = vb.run(state, today=today)
        return state, sent

    def test_first_run_seeds_edition_silently(self):
        state, sent = self._run({}, INDEX_HTML_JULY, BULLETIN_HTML,
                                _dt.date(2026, 7, 5))
        self.assertEqual(state[vb.EDITION_KEY], "2026-07")
        titles = [p["title"] for p in sent]
        self.assertFalse(any(t.startswith("New visa bulletin") for t in titles))
        self.assertNotIn("Visa bulletin is late", titles)
        # The existing first-seen cell pushes still fire on day one.
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("First seen" in p["message"] for p in sent))

    def test_new_bulletin_with_date_change_is_one_combined_push(self):
        state, sent = self._run(dict(_TRACKING_JULY), INDEX_HTML_AUGUST,
                                BULLETIN_HTML_ADVANCED, _dt.date(2026, 7, 12))
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
        state, sent = self._run(dict(_TRACKING_JULY), INDEX_HTML_AUGUST,
                                BULLETIN_HTML, _dt.date(2026, 7, 12))
        self.assertEqual(state[vb.EDITION_KEY], "2026-08")
        self.assertEqual(len(sent), 1)
        push = sent[0]
        self.assertEqual(push["title"], "New visa bulletin: August 2026")
        self.assertEqual(push["severity"], "moderate")
        self.assertIn("held steady at 08NOV08", push["message"])
        self.assertIn("held steady at 22DEC09", push["message"])

    def test_no_new_bulletin_before_the_15th_is_silent(self):
        state, sent = self._run(dict(_TRACKING_JULY), INDEX_HTML_JULY,
                                BULLETIN_HTML, _dt.date(2026, 7, 10))
        self.assertEqual(sent, [])
        self.assertEqual(state[vb.EDITION_KEY], "2026-07")
        self.assertNotIn(vb.LATE_KEY, state)

    def test_late_alert_fires_once_then_rearms_on_new_bulletin(self):
        # July 16: still no August bulletin -> exactly one late alert.
        state, sent = self._run(dict(_TRACKING_JULY), INDEX_HTML_JULY,
                                BULLETIN_HTML, _dt.date(2026, 7, 16))
        self.assertEqual([p["title"] for p in sent], ["Visa bulletin is late"])
        self.assertEqual(state[vb.LATE_KEY], "2026-08")

        # July 17: still late, but the alert must not repeat.
        state, sent = self._run(state, INDEX_HTML_JULY, BULLETIN_HTML,
                                _dt.date(2026, 7, 17))
        self.assertEqual(sent, [])

        # July 20: the August bulletin lands -> new-bulletin push, no late alert.
        state, sent = self._run(state, INDEX_HTML_AUGUST, BULLETIN_HTML,
                                _dt.date(2026, 7, 20))
        self.assertEqual([p["title"] for p in sent],
                         ["New visa bulletin: August 2026"])


if __name__ == "__main__":
    unittest.main()
