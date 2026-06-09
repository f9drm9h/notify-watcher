"""Tests for the Visa Bulletin parser (notify_watcher.topics.visa_bulletin).

Pins _table_after_heading + _f4_all_other against a captured-shape bulletin page
(two family-sponsored tables: Final Action Dates and Dates for Filing, each with
the F4 "All Chargeability Areas Except Those Listed" cell). travel.state.gov
quietly restructures these tables; without this test such a change would only
show up as a "F4 row not found" log line and a silently missed alert.
"""
from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from notify_watcher.topics import visa_bulletin as vb

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


if __name__ == "__main__":
    unittest.main()
