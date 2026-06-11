"""Tests for the EDESUR scheduled-outage watcher (notify_watcher.topics.outages)."""
from __future__ import annotations

import datetime as dt
import unittest

from notify_watcher.topics import outages

# Trimmed to the structural skeleton of the real page: day-tab buttons paired
# to tab panes by pill id, a province accordion per pane, and h5/p pairs of
# time window + affected zones inside each accordion body.
PAGE_HTML = """
<ul class="nav nav-pills" id="pills-tab">
  <li><button class="nav-link day-tag active" id="pills-aaa-tab">
    <h5 class="mb-0">s&#225;bado</h5>
    <small class="text-orangesur">06 de junio, 2026</small>
  </button></li>
  <li><button class="nav-link day-tag " id="pills-bbb-tab">
    <h5 class="mb-0">mi&#233;rcoles</h5>
    <small class="text-orangesur">10 de junio, 2026</small>
  </button></li>
</ul>
<div class="tab-content">
  <div class="tab-pane fade show active" id="pills-aaa">
    <div class="accordion-item">
      <h2 class="accordion-header">
        <button class="accordion-button document"><h4 class="mb-0">
          Santo Domingo
        </h4></button>
      </h2>
      <div class="accordion-body">
        <h5 class="title-zona">Zonas en mantenimiento 08:00 a. m. a 12:00 p. m.</h5>
        <p><p>Buenas Noches, Santa Rosa, Palav&#233;</p></p>
      </div>
    </div>
    <div class="accordion-item">
      <h2 class="accordion-header">
        <button class="accordion-button document"><h4 class="mb-0">
          Azua
        </h4></button>
      </h2>
      <div class="accordion-body">
        <h5 class="title-zona">Zonas en mantenimiento 10:00 a. m. a 03:00 p. m.</h5>
        <p><p>La Palmita, Nuevo</p></p>
      </div>
    </div>
  </div>
  <div class="tab-pane fade " id="pills-bbb">
    <div class="accordion-item">
      <h2 class="accordion-header">
        <button class="accordion-button document"><h4 class="mb-0">
          Distrito Nacional
        </h4></button>
      </h2>
      <div class="accordion-body">
        <h5 class="title-zona">Zonas en mantenimiento 10:00 a. m. a 12:00 p. m.</h5>
        <p><p>Zona Universitaria (parcial), La Esperilla (parcial)</p></p>
        <h5 class="title-zona">Zonas en mantenimiento 10:00 a. m. a 03:00 p. m.</h5>
        <p><p>La Esperilla (parcial)</p></p>
      </div>
    </div>
  </div>
</div>
"""


class ParseDateTest(unittest.TestCase):
    def test_parses_the_pages_format(self):
        self.assertEqual(outages._parse_date_es("06 de junio, 2026"), dt.date(2026, 6, 6))

    def test_tolerates_surrounding_text_and_no_comma(self):
        self.assertEqual(outages._parse_date_es("sábado 6 de junio 2026"), dt.date(2026, 6, 6))

    def test_unknown_month_or_garbage_is_none(self):
        self.assertIsNone(outages._parse_date_es("06 de junio"))
        self.assertIsNone(outages._parse_date_es("06 de juniembre, 2026"))
        self.assertIsNone(outages._parse_date_es(""))


class RegionMatchTest(unittest.TestCase):
    REGIONS = ["Santo Domingo", "Distrito Nacional"]

    def test_matches_ignore_case_and_accents(self):
        self.assertTrue(outages._matches_region("SANTO DOMINGO", self.REGIONS))
        self.assertTrue(outages._matches_region("Distrito Nacional", ["distrito nacional"]))

    def test_other_provinces_do_not_match(self):
        self.assertFalse(outages._matches_region("Azua", self.REGIONS))
        self.assertFalse(outages._matches_region("Barahona", self.REGIONS))


class ParsePageTest(unittest.TestCase):
    def test_extracts_every_notice_with_its_date(self):
        rows = outages._parse_page(PAGE_HTML)
        self.assertEqual(len(rows), 4)
        first = rows[0]
        self.assertEqual(first["date"], dt.date(2026, 6, 6))
        self.assertEqual(first["province"], "Santo Domingo")
        self.assertEqual(first["window"], "08:00 a. m. a 12:00 p. m.")
        self.assertIn("Santa Rosa", first["zones"])

    def test_a_province_can_carry_several_windows(self):
        rows = [r for r in outages._parse_page(PAGE_HTML)
                if r["province"] == "Distrito Nacional"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["date"] for r in rows}, {dt.date(2026, 6, 10)})
        self.assertEqual(rows[0]["window"], "10:00 a. m. a 12:00 p. m.")
        self.assertEqual(rows[1]["window"], "10:00 a. m. a 03:00 p. m.")

    def test_empty_or_unrelated_html_yields_nothing(self):
        self.assertEqual(outages._parse_page(""), [])
        self.assertEqual(outages._parse_page("<html><body><p>hola</p></body></html>"), [])


class DedupKeyTest(unittest.TestCase):
    def test_same_notice_same_key_different_notice_different_key(self):
        row = {"date": dt.date(2026, 6, 10), "province": "Santo Domingo",
               "window": "08:00 a 12:00", "zones": "Palavé"}
        self.assertEqual(outages._key(row), outages._key(dict(row)))
        other = dict(row, window="09:00 a 12:00")
        self.assertNotEqual(outages._key(row), outages._key(other))


class DueTest(unittest.TestCase):
    OUTAGE = dt.date(2026, 6, 10)

    def test_day_before_is_due(self):
        self.assertTrue(outages._due(self.OUTAGE, dt.date(2026, 6, 9), lead_days=1))

    def test_day_of_is_due(self):
        self.assertTrue(outages._due(self.OUTAGE, dt.date(2026, 6, 10), lead_days=1))

    def test_too_early_is_not_due(self):
        self.assertFalse(outages._due(self.OUTAGE, dt.date(2026, 6, 8), lead_days=1))

    def test_after_the_outage_is_never_due(self):
        self.assertFalse(outages._due(self.OUTAGE, dt.date(2026, 6, 11), lead_days=1))

    def test_longer_lead_widens_the_window(self):
        self.assertTrue(outages._due(self.OUTAGE, dt.date(2026, 6, 7), lead_days=3))


if __name__ == "__main__":
    unittest.main()
