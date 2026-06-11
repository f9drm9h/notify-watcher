"""Tests for the ITSC academic-calendar watcher (notify_watcher.topics.itsc)."""
from __future__ import annotations

import datetime as dt
import unittest

from notify_watcher.topics import itsc

PAGE_HTML = """
<a href="https://www.itsc.edu.do/wp-content/uploads/2026/02/Manual_Cambio_Contrasena_ITSC.pdf">Manual</a>
<a href="https://www.itsc.edu.do/wp-content/uploads/2026/01/Calendario-academico-2026-1-ENERO-ABRIL.pdf">Descargar</a>
<a href="https://www.itsc.edu.do/wp-content/uploads/2026/05/Calendario-academico-2026-2-MAYO-AGOSTO-_-Actualizado_.pdf">Descargar Calendario</a>
"""

# Trimmed from the real PDF's layout text: a title, the column header, ranged
# rows, single-date rows, and a wrapped-name fragment ("2026") with no date.
PDF_TEXT = """
                     CALENDARIO ACADÉMICO ACTUALIZADO
        PROCESO / ACTIVIDAD                    INICIO              FIN
Solicitud Admisión en Línea (General)          5/5/2026            23/6/2026
Feriado Corpus Christi                         4/6/2026
Solicitud Cambio de Carrera / Reingreso 2026-3 / Readmisión    9/6/2026    13/6/2026
2026
Evaluación Competencias Inglés/Francés (Examen)    22/7/2026     22/7/2026
"""


class CalendarLinksTest(unittest.TestCase):
    def test_finds_calendar_pdfs_newest_first(self):
        links = itsc._calendar_links(PAGE_HTML)
        self.assertEqual(len(links), 2)  # the password manual is not a calendar
        self.assertIn("2026-2-MAYO-AGOSTO", links[0])  # uploads/2026/05 beats /01
        self.assertIn("2026-1-ENERO-ABRIL", links[1])

    def test_relative_hrefs_are_made_absolute(self):
        html = '<a href="/wp-content/uploads/2026/05/calendario-academico.pdf">x</a>'
        self.assertEqual(itsc._calendar_links(html),
                         ["https://www.itsc.edu.do/wp-content/uploads/2026/05/"
                          "calendario-academico.pdf"])

    def test_unrelated_html_yields_nothing(self):
        self.assertEqual(itsc._calendar_links("<html><body>hola</body></html>"), [])


class ParseDmyTest(unittest.TestCase):
    def test_parses_day_first(self):
        self.assertEqual(itsc._parse_dmy("30/4/2026"), dt.date(2026, 4, 30))

    def test_garbage_is_none(self):
        self.assertIsNone(itsc._parse_dmy("30/13/2026"))
        self.assertIsNone(itsc._parse_dmy("2026-04-30"))
        self.assertIsNone(itsc._parse_dmy(""))


class ParseRowsTest(unittest.TestCase):
    def test_extracts_ranged_and_single_date_rows(self):
        rows = itsc._parse_rows(PDF_TEXT)
        self.assertEqual(len(rows), 4)  # header/title/orphan "2026" skipped
        self.assertEqual(rows[0], {"activity": "Solicitud Admisión en Línea (General)",
                                   "start": dt.date(2026, 5, 5),
                                   "end": dt.date(2026, 6, 23)})
        self.assertEqual(rows[1], {"activity": "Feriado Corpus Christi",
                                   "start": dt.date(2026, 6, 4), "end": None})

    def test_a_year_token_inside_the_activity_is_not_a_date(self):
        rows = itsc._parse_rows(PDF_TEXT)
        self.assertEqual(rows[2]["activity"],
                         "Solicitud Cambio de Carrera / Reingreso 2026-3 / Readmisión")
        self.assertEqual(rows[2]["start"], dt.date(2026, 6, 9))

    def test_empty_text_yields_nothing(self):
        self.assertEqual(itsc._parse_rows(""), [])


class BoundariesTest(unittest.TestCase):
    def test_a_period_alerts_at_both_ends(self):
        row = {"activity": "x", "start": dt.date(2026, 5, 5), "end": dt.date(2026, 6, 23)}
        self.assertEqual(itsc._boundaries(row),
                         [("starts", dt.date(2026, 5, 5)), ("ends", dt.date(2026, 6, 23))])

    def test_single_date_and_same_day_ranges_alert_once(self):
        single = {"activity": "x", "start": dt.date(2026, 6, 4), "end": None}
        self.assertEqual(itsc._boundaries(single), [("on", dt.date(2026, 6, 4))])
        same = {"activity": "x", "start": dt.date(2026, 7, 22), "end": dt.date(2026, 7, 22)}
        self.assertEqual(itsc._boundaries(same), [("on", dt.date(2026, 7, 22))])


class DueTest(unittest.TestCase):
    ROWS = itsc._parse_rows(PDF_TEXT)

    def test_seven_days_before_a_deadline_is_due(self):
        due = itsc._due(self.ROWS, dt.date(2026, 6, 16), [7, 1])
        self.assertEqual([(r["activity"], lbl, n) for _, r, lbl, _, n in due],
                         [("Solicitud Admisión en Línea (General)", "ends", 7)])

    def test_one_day_before_is_due_with_its_own_key(self):
        due7 = itsc._due(self.ROWS, dt.date(2026, 6, 16), [7, 1])
        due1 = itsc._due(self.ROWS, dt.date(2026, 6, 22), [7, 1])
        self.assertEqual(len(due1), 1)
        self.assertEqual(due1[0][4], 1)
        self.assertNotEqual(due7[0][0], due1[0][0])  # 7d and 1d each fire once

    def test_other_days_are_quiet(self):
        self.assertEqual(itsc._due(self.ROWS, dt.date(2026, 6, 18), [7, 1]), [])

    def test_day_of_is_not_due_with_default_leads(self):
        self.assertEqual(itsc._due(self.ROWS, dt.date(2026, 6, 23), [7, 1]), [])

    def test_key_is_stable(self):
        a = itsc._due(self.ROWS, dt.date(2026, 6, 16), [7, 1])[0][0]
        b = itsc._due(self.ROWS, dt.date(2026, 6, 16), [7, 1])[0][0]
        self.assertEqual(a, b)


class WhenPhraseTest(unittest.TestCase):
    def test_phrases(self):
        self.assertEqual(itsc._when_phrase(0), "today")
        self.assertEqual(itsc._when_phrase(1), "tomorrow")
        self.assertEqual(itsc._when_phrase(7), "in 7 days")


if __name__ == "__main__":
    unittest.main()
