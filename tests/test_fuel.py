"""Tests for the DR weekly fuel-price topic (notify_watcher.topics.fuel)."""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
