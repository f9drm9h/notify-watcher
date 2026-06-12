"""Tests for the scheduled-outage watcher (notify_watcher.topics.outages):
EDEESTE's weekly PDF (primary — home is Hainamosa, Santo Domingo Este) and
EDESUR's weekly HTML page (kept, off by default)."""
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


# --------------------------------------------------------------------------
# EDEESTE: weekly PDF behind the WordPress download archive
# --------------------------------------------------------------------------

ARCHIVE_HTML = """
<div class="media-body package-title">
  <a href="https://edeeste.com.do/index.php/download/semana-08-junio/">Desde el Lunes 08 de Junio hasta el Domingo 14 Junio 2026</a>
</div>
<div class="media-body package-title">
  <a href="https://edeeste.com.do/index.php/download/semana-01-junio/">Desde el Lunes 01 hasta el Domingo 07 de Junio 2026</a>
</div>
"""

WEEK = (dt.date(2026, 6, 8), dt.date(2026, 6, 14))


def _two_panel(rows: list[tuple[str, str]]) -> str:
    """Lay rows out like the real PDF: left panel at column 0, right at 60."""
    return "\n".join(f"{left:<60}{right}" for left, right in rows)


# Mirrors the real PDF's hostile layout: side-by-side day panels, and the
# right panel's header carries the WRONG month (mayo inside a June week) —
# exactly what EDEESTE published for the June 8-14 PDF.
PDF_TEXT = _two_panel([
    ("LUNES 8 DE JUNIO DEL 2026",          "JUEVES 11 DE MAYO DEL 2026"),
    ("SANTO DOMINGO  LOS MINA 69, NO 2",    "HAINAMOSA, NO 6    HAMO06"),
    ("9:20 a.m.   3:20 p.m.   Borrador",    "9:20 a.m.    3:20 p.m."),
    ("  ver jueves 11 de junio (nota)",     "Farallones, Villa Eloisa II"),
    ("MARTES 9 DE JUNIO DEL 2026",          ""),
    ("LA ROMANA, NO 3",                     ""),
])


class ParsePackagesTest(unittest.TestCase):
    def test_extracts_links_and_titles_newest_first(self):
        pkgs = outages._parse_packages(ARCHIVE_HTML)
        self.assertEqual(len(pkgs), 2)
        self.assertEqual(pkgs[0][0], "https://edeeste.com.do/index.php/download/semana-08-junio/")
        self.assertEqual(pkgs[0][1], "Desde el Lunes 08 de Junio hasta el Domingo 14 Junio 2026")

    def test_unrelated_html_yields_nothing(self):
        self.assertEqual(outages._parse_packages("<html><body>hola</body></html>"), [])


class ParseWeekRangeTest(unittest.TestCase):
    def test_month_named_twice(self):
        self.assertEqual(
            outages._parse_week_range("Desde el Lunes 08 de Junio hasta el Domingo 14 Junio 2026"),
            (dt.date(2026, 6, 8), dt.date(2026, 6, 14)))

    def test_month_named_once(self):
        self.assertEqual(
            outages._parse_week_range("Desde el Lunes 04 hasta el Domingo 10 de Mayo 2026"),
            (dt.date(2026, 5, 4), dt.date(2026, 5, 10)))

    def test_new_year_week_rolls_the_start_back(self):
        self.assertEqual(
            outages._parse_week_range("Desde el Lunes 29 de Diciembre hasta el Domingo 04 de Enero 2026"),
            (dt.date(2025, 12, 29), dt.date(2026, 1, 4)))

    def test_garbage_is_none(self):
        self.assertIsNone(outages._parse_week_range("Programa de mantenimiento"))
        self.assertIsNone(outages._parse_week_range(""))


class ResolveDayTest(unittest.TestCase):
    def test_day_number_beats_the_month_word(self):
        # The whole point: a "JUEVES 11 DE MAYO" header inside the June 8-14
        # PDF must resolve to June 11.
        self.assertEqual(outages._resolve_day(11, *WEEK), dt.date(2026, 6, 11))

    def test_day_outside_the_week_is_none(self):
        self.assertIsNone(outages._resolve_day(20, *WEEK))


class ScanPdfTextTest(unittest.TestCase):
    def test_zone_found_with_date_from_day_number_and_window(self):
        hits = outages._scan_pdf_text(PDF_TEXT, *WEEK, zones=["Hainamosa"])
        self.assertEqual(hits, [{
            "date": dt.date(2026, 6, 11),  # despite the "mayo" header
            "zone": "Hainamosa",
            "window": "9:20 a.m. a 3:20 p.m.",
        }])

    def test_matching_is_accent_and_case_insensitive(self):
        hits = outages._scan_pdf_text(PDF_TEXT, *WEEK, zones=["haïnamosa"])
        self.assertEqual(len(hits), 1)

    def test_day_mention_in_body_text_does_not_open_a_section(self):
        # "ver jueves 11 de junio (nota)" sits indented inside Monday's
        # section; only Hainamosa's real (right-panel) section may match.
        hits = outages._scan_pdf_text(PDF_TEXT, *WEEK, zones=["Los Mina"])
        self.assertEqual([h["date"] for h in hits], [dt.date(2026, 6, 8)])

    def test_unwatched_zone_yields_nothing(self):
        self.assertEqual(outages._scan_pdf_text(PDF_TEXT, *WEEK, zones=["Gualey"]), [])


class EdeesteKeyTest(unittest.TestCase):
    def test_key_is_stable_and_fold_insensitive(self):
        d = dt.date(2026, 6, 11)
        self.assertEqual(outages._edeeste_key(d, "Hainamosa"),
                         outages._edeeste_key(d, "HAINAMOSA"))

    def test_different_day_different_key(self):
        self.assertNotEqual(outages._edeeste_key(dt.date(2026, 6, 10), "Hainamosa"),
                            outages._edeeste_key(dt.date(2026, 6, 11), "Hainamosa"))


class HealthContractTest(unittest.TestCase):
    """run() aggregates per-source outcomes into one health report."""

    EDEESTE_CFG = {"edeeste": {"zones": ["Hainamosa"]}}

    def test_unconfigured_makes_no_claim(self):
        from unittest import mock
        from notify_watcher import health
        with mock.patch.object(outages.config, "section", return_value={}):
            state = outages.run({})
        self.assertNotIn(health.STATUS_KEY, state)

    def test_all_sources_failing_reports_source_failed(self):
        from unittest import mock
        from notify_watcher import health
        with mock.patch.object(outages.config, "section",
                               return_value=self.EDEESTE_CFG), \
                mock.patch.object(outages, "_edeeste_collect", return_value=None):
            state = outages.run({})
        status = state[health.STATUS_KEY]["outages"]
        self.assertTrue(status["source_failed"])
        self.assertIn("EDEESTE", status["message"])

    def test_one_delivering_source_reports_ok(self):
        from unittest import mock
        from notify_watcher import health
        rows = [{"date": dt.date(2026, 6, 11), "zone": "Hainamosa",
                 "window": "", "url": "https://edeeste.example/x"}]
        with mock.patch.object(outages.config, "section",
                               return_value=self.EDEESTE_CFG), \
                mock.patch.object(outages, "_edeeste_collect", return_value=rows):
            state = outages.run({})  # first sight: seeds silently
        status = state[health.STATUS_KEY]["outages"]
        self.assertTrue(status["ok"])
        self.assertEqual(status["data_count"], 1)


if __name__ == "__main__":
    unittest.main()
