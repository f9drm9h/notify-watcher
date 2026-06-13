"""Tests for the DR weekly fuel-price topic (notify_watcher.topics.fuel)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from notify_watcher import health
from notify_watcher.topics import fuel

# Trimmed from the real text pypdf extracts from a MICM weekly notice
# (corte 06-12 JUN 2026): prose preamble, the per-fuel rows (official price is
# the row maximum; parenthesized values are the week's downward variations),
# the EGP power-generation variants we must NOT confuse with consumer fuels,
# and the GLP block whose row appends a post-adjustment final price.
SAMPLE_TEXT = """
AVISO
El Ministerio de Industria, Comercio y MiPymes (MICM) dispone mediante el presente
aviso los precios oficiales de los combustibles que regirán a partir de la 00:00 hora.
TIPO *PRECIO PRECIO AJUSTE VARIACION
Gasolina Premium 187.60 71.85 30.01 16.59 27.07 6.68 339.80 (4.70) 0.00
Gasolina Regular 170.72 63.83 27.31 16.59 27.07 6.68 312.20 (4.70) 0.00
Gasoil Regular 164.08 28.06 26.25 14.28 23.75 6.68 263.10 (3.30) 0.00
Gasoil Regular EGP-C ( Inter. y No Interconectado) 204.57 28.06 32.73 5.24 0.00 6.68 277.28 0.00 (9.03)
Gasoil Optimo 181.41 34.53 29.03 14.52 24.03 6.68 290.20 (3.10) 0.00
Avtur 227.80 6.30 14.81 15.53 0.00 6.68 271.12 0.00 (5.90)
Kerosene 225.54 17.99 36.08 9.10 17.01 6.68 312.40 (4.10) (6.40)
Fuel Oil 140.20 17.99 22.43 1.54 0.00 6.68 188.84 0.00 (5.22)
Gas Licuado de Petróleo (GLP) ** 86.30 0.00 13.81 11.71 17.90 6.68 136.40 0.00 0.80 137.20 0.00
Cilindros de 100 Libras (25.00 Gls. Max.)*** 3,429.95
Tasa de Cambio Promedio-Mercado Bancario, aplicada para todos los combustibles RD$58.70
"""


class ParsePricesTest(unittest.TestCase):
    def test_official_prices_extracted(self):
        prices = fuel._parse_prices(SAMPLE_TEXT)
        self.assertEqual(prices["Gasolina Premium"], 339.80)
        self.assertEqual(prices["Gasolina Regular"], 312.20)
        self.assertEqual(prices["Gasoil Óptimo"], 290.20)
        self.assertEqual(prices["Kerosene"], 312.40)

    def test_gasoil_regular_ignores_egp_rows(self):
        # The EGP-C variant (277.28) must not shadow the consumer price.
        self.assertEqual(fuel._parse_prices(SAMPLE_TEXT)["Gasoil Regular"], 263.10)

    def test_glp_picks_post_adjustment_price(self):
        self.assertEqual(fuel._parse_prices(SAMPLE_TEXT)["GLP"], 137.20)

    def test_untracked_and_prose_lines_ignored(self):
        prices = fuel._parse_prices(SAMPLE_TEXT)
        self.assertEqual(set(prices), {n for n, _ in fuel.FUELS})

    def test_empty_text_yields_nothing(self):
        self.assertEqual(fuel._parse_prices(""), {})


class FindPdfTest(unittest.TestCase):
    def test_first_notice_link_wins(self):
        html = (
            '<a href="https://micm.gob.do/wp-content/uploads/2026/06/'
            'AVISO-PRE.-SEM.CORTE-06-12-JUN-DE-2026-5-CON.pdf">corte 06-12</a>'
            '<a href="https://micm.gob.do/wp-content/uploads/2026/05/'
            'AVISO-PRE.-SEM.CORTE-30-MAY-05-JUN-DE-2026-.pdf">corte 30-05</a>'
        )
        self.assertIn("06-12-JUN-DE-2026", fuel._find_pdf(html))

    def test_no_notice_returns_none(self):
        self.assertIsNone(fuel._find_pdf("<html><a href='/x.pdf'>other</a></html>"))


class EvaluateTest(unittest.TestCase):
    CUR = {"Gasolina Premium": 339.80, "GLP": 137.20}

    def test_small_move_digests_with_magnitude(self):
        prev = {"Gasolina Premium": 344.50, "GLP": 137.20}
        action, body, biggest = fuel._evaluate(prev, self.CUR, 5.0)
        self.assertEqual(action, "digest")
        self.assertIn("Gasolina Premium: RD$339.80 (-4.70, -1.4%)", body)
        self.assertIn("GLP: RD$137.20 (sin cambio)", body)
        self.assertEqual(biggest.current, 339.80)

    def test_big_move_pushes(self):
        prev = {"Gasolina Premium": 300.00, "GLP": 137.20}
        action, _, biggest = fuel._evaluate(prev, self.CUR, 5.0)
        self.assertEqual(action, "push")
        self.assertAlmostEqual(biggest.metadata["pct_delta"], 13.27, places=2)

    def test_flat_week_digests_without_change(self):
        action, body, biggest = fuel._evaluate(dict(self.CUR), self.CUR, 5.0)
        self.assertEqual(action, "digest")
        self.assertIsNone(biggest)
        self.assertIn("sin cambio", body)

    def test_threshold_is_inclusive(self):
        prev = {"Gasolina Premium": 339.80, "GLP": 130.00}  # +5.54% on GLP
        action, _, _ = fuel._evaluate(prev, self.CUR, 5.0)
        self.assertEqual(action, "push")
        action, _, _ = fuel._evaluate(prev, self.CUR, 6.0)
        self.assertEqual(action, "digest")


PDF_URL = ("https://micm.gob.do/wp-content/uploads/2026/06/"
           "AVISO-PRE.-SEM.CORTE-06-12-JUN-DE-2026-5-CON.pdf")
LISTING_HTML = f'<a href="{PDF_URL}">corte 06-12</a>'


class _Resp:
    def __init__(self, text="", content=b""):
        self.text, self.content = text, content

    def raise_for_status(self):
        pass


class RunHealthContractTest(unittest.TestCase):
    """run() must report its source outcome via the topic health contract."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"NOTIFY_DAILY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _status(self, state):
        return (state.get(health.STATUS_KEY) or {}).get("fuel")

    def test_listing_fetch_failure_reports_source_failed(self):
        state: dict = {}
        with mock.patch.object(fuel.requests, "get",
                               side_effect=OSError("connection refused")):
            state = fuel.run(state)
        status = self._status(state)
        self.assertTrue(status["source_failed"])
        self.assertIn("listing fetch failed", status["message"])

    def test_listing_without_notice_reports_source_failed(self):
        state: dict = {}
        with mock.patch.object(fuel.requests, "get",
                               return_value=_Resp(text="<html>no links</html>")):
            state = fuel.run(state)
        status = self._status(state)
        self.assertTrue(status["source_failed"])
        self.assertIn("no weekly notice PDF", status["message"])

    def test_known_notice_is_a_true_success(self):
        # The listing/PDF answering with the already-seen content is healthy.
        pdf_bytes = b"same notice"
        prices = fuel._parse_prices(SAMPLE_TEXT)
        state = {
            fuel.LAST_PDF_KEY: PDF_URL,
            fuel.LAST_PDF_HASH_KEY: fuel._hash_pdf(pdf_bytes),
            fuel.STATE_KEY: prices,
        }
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=pdf_bytes)]):
            state = fuel.run(state)
        status = self._status(state)
        self.assertTrue(status["ok"])
        self.assertEqual(status["data_count"], len(prices))
        self.assertIn("last_data", state["topic_health"]["fuel"])
        self.assertIn(fuel.LAST_PRICES_SEEN_AT_KEY, state)

    def test_unparseable_notice_reports_source_failed(self):
        state: dict = {}
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value="no table here"))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML), _Resp()]), \
                mock.patch("pypdf.PdfReader", return_value=page):
            state = fuel.run(state)
        status = self._status(state)
        self.assertTrue(status["source_failed"])
        self.assertIn("no prices parsed", status["message"])
        self.assertNotIn(fuel.LAST_PDF_KEY, state)  # dedup key kept for a retry
        self.assertNotIn(fuel.LAST_PDF_HASH_KEY, state)

    def test_new_notice_reports_ok_with_price_count(self):
        state: dict = {}
        pdf_bytes = b"new notice"
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=SAMPLE_TEXT))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=pdf_bytes)]), \
                mock.patch("pypdf.PdfReader", return_value=page):
            state = fuel.run(state)
        status = self._status(state)
        self.assertTrue(status["ok"])
        self.assertEqual(status["data_count"], len(fuel.FUELS))
        self.assertIn("last_data", state["topic_health"]["fuel"])
        self.assertEqual(state[fuel.LAST_PDF_KEY], PDF_URL)
        self.assertEqual(state[fuel.LAST_PDF_HASH_KEY], fuel._hash_pdf(pdf_bytes))
        self.assertIn(fuel.LAST_PRICES_SEEN_AT_KEY, state)

    def test_same_url_changed_prices_are_processed(self):
        old_prices = fuel._parse_prices(SAMPLE_TEXT)
        old_prices["Gasolina Premium"] = 330.00
        state = {
            fuel.LAST_PDF_KEY: PDF_URL,
            fuel.LAST_PDF_HASH_KEY: fuel._hash_pdf(b"old notice"),
            fuel.STATE_KEY: old_prices,
        }
        pdf_bytes = b"changed notice"
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=SAMPLE_TEXT))]

        def section(name):
            return {"push_pct": 50.0} if name == "fuel" else {}

        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=pdf_bytes)]), \
                mock.patch("pypdf.PdfReader", return_value=page), \
                mock.patch.object(fuel.config, "section", side_effect=section):
            state = fuel.run(state)

        self.assertTrue(self._status(state)["ok"])
        self.assertEqual(state[fuel.LAST_PDF_KEY], PDF_URL)
        self.assertEqual(state[fuel.LAST_PDF_HASH_KEY], fuel._hash_pdf(pdf_bytes))
        self.assertEqual(state[fuel.STATE_KEY]["Gasolina Premium"], 339.80)
        buf = state.get("digest_buffer") or []
        self.assertEqual(len(buf), 1)
        self.assertTrue(buf[0]["preserve_detail"])
        self.assertIn("Gasolina Premium: RD$339.80", buf[0]["detail"])
        self.assertIn("GLP: RD$137.20", buf[0]["detail"])

    def test_same_url_without_hash_parses_but_unchanged_prices_do_not_redigest(self):
        prices = fuel._parse_prices(SAMPLE_TEXT)
        state = {fuel.LAST_PDF_KEY: PDF_URL, fuel.STATE_KEY: prices}
        pdf_bytes = b"same notice with newly stored hash"
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=SAMPLE_TEXT))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=pdf_bytes)]), \
                mock.patch("pypdf.PdfReader", return_value=page):
            state = fuel.run(state)

        self.assertTrue(self._status(state)["ok"])
        self.assertEqual(state[fuel.LAST_PDF_HASH_KEY], fuel._hash_pdf(pdf_bytes))
        self.assertNotIn("digest_buffer", state)

    def test_gated_run_makes_no_claim(self):
        with mock.patch.dict(os.environ, {"NOTIFY_DAILY": ""}):
            state = fuel.run({})
        self.assertIsNone(self._status(state))


if __name__ == "__main__":
    unittest.main()
