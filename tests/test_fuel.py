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


# What pypdf ACTUALLY returns for a live MICM notice (corte 01-07 AGO 2026,
# trimmed): the whole price table on ONE line, with no separator between a
# fuel's name and its first figure or between adjacent figures. Parsing this
# form line-by-line is what produced six identical RD$3,380.07 prices — the
# "Cilindros de 100 Libras" cylinder price, the largest number on the line — for
# the entire life of the topic. Keep this fixture byte-real: an idealized
# one-row-per-line sample is exactly what let the bug ship green.
COLLAPSED_TEXT = (
    "TIPO *PRECIO PRECIOAJUSTE VARIACIONDEPARIDADLEY 112-00LEY 495-06"
    "DISTRIBUIDORDETALLISTACOMISIONOFICIALPORAUM/ (DISM.)COMBUSTIBLE"
    "IMPORTACIONAD-VALOREMTRANSPORTE(RD$/GL)RESOL NO.(RD$/GL.)"
    "REFORMA FISCAL201-1416%AVTUR  6.5%"
    "Gasolina Premium191.0471.8530.5716.5927.076.68343.80(5.70)0.00"
    "Gasolina Regular167.0163.8326.7216.5927.076.68307.90(5.40)0.00"
    "Gasoil Regular159.9428.0625.5914.2823.756.68258.30(3.50)0.00"
    "Gasoil Regular EGP-C ( Inter. y No Interconectado)"
    "238.7628.0638.205.240.006.68316.940.0012.66"
    "Gasoil Regular EGP-T ( Inter. y No Interconectado)"
    "238.7628.0638.205.240.000.00310.260.0012.66"
    "Gasoil Optimo184.5234.5329.5214.5224.036.68293.80(3.70)0.00"
    "Avtur 247.666.3016.1015.530.006.68292.270.008.41"
    "Kerosene245.4617.9939.269.1017.016.68335.50(4.50)8.60"
    "Fuel Oil123.6117.9919.781.540.006.68169.600.000.06"
    "Gas Licuado de Petróleo (GLP) **"
    "84.580.0013.5311.7117.906.68134.400.000.80135.200.00"
    "Cilindros de 100 Libras (25.00 Gls. Max.)***3,380.07"
    "Cilindros de 50 Libras (12.50 Gls. Max.)1,690.04"
)

# The exact poison the broken parser banked in state.json.
COLLAPSE_VICTIM = {name: 3380.07 for name, _ in fuel.FUELS}


class CollapsedTableTest(unittest.TestCase):
    """The real-world layout: no line breaks anywhere in the table."""

    def test_each_fuel_gets_its_own_price(self):
        prices = fuel._parse_prices(COLLAPSED_TEXT)
        self.assertEqual(prices, {
            "Gasolina Premium": 343.80,
            "Gasolina Regular": 307.90,
            "Gasoil Regular": 258.30,
            "Gasoil Óptimo": 293.80,
            "Kerosene": 335.50,
            "GLP": 135.20,
        })

    def test_cylinder_price_never_leaks_into_a_fuel(self):
        # The regression itself: 3,380.07 is the Cilindros row, not a fuel.
        self.assertNotIn(3380.07, fuel._parse_prices(COLLAPSED_TEXT).values())

    def test_egp_rows_do_not_shadow_consumer_gasoil(self):
        # 316.94 / 310.26 are the power-generation variants on the same line.
        self.assertEqual(fuel._parse_prices(COLLAPSED_TEXT)["Gasoil Regular"], 258.30)

    def test_kerosene_stops_before_fuel_oil(self):
        self.assertEqual(fuel._parse_prices(COLLAPSED_TEXT)["Kerosene"], 335.50)


class ValidateTest(unittest.TestCase):
    def test_good_parse_passes_through_unchanged(self):
        prices = fuel._parse_prices(SAMPLE_TEXT)
        self.assertEqual(fuel._validate(prices), (prices, ""))

    def test_all_identical_prices_are_rejected(self):
        clean, reason = fuel._validate({"Gasolina Premium": 300.0,
                                        "Gasolina Regular": 300.0,
                                        "Kerosene": 300.0})
        self.assertEqual(clean, {})
        self.assertIn("identical", reason)

    def test_two_matching_prices_are_allowed(self):
        # Premium and Regular CAN coincide; only 3+ is the collapse signature.
        pair = {"Gasolina Premium": 300.0, "Gasolina Regular": 300.0}
        self.assertEqual(fuel._validate(pair), (pair, ""))

    def test_out_of_band_price_is_dropped_not_fatal(self):
        clean, reason = fuel._validate({"Gasolina Premium": 343.80,
                                        "Gasolina Regular": 307.90,
                                        "GLP": 3380.07})
        self.assertEqual(reason, "")
        self.assertEqual(set(clean), {"Gasolina Premium", "Gasolina Regular"})

    def test_the_original_bugs_output_is_rejected(self):
        clean, reason = fuel._validate(COLLAPSE_VICTIM)
        self.assertEqual(clean, {})
        self.assertTrue(reason)


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

    def test_implausible_parse_reports_source_failed(self):
        # A notice that parses but yields nonsense must reach the watchdog, not
        # the digest. This is the case the topic used to swallow every week.
        collapsed = ("Gasolina Premium 3,380.07 3,380.07 3,380.07\n"
                     "Gasolina Regular 3,380.07 3,380.07 3,380.07\n"
                     "Kerosene 3,380.07 3,380.07 3,380.07\n")
        state: dict = {}
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=collapsed))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML), _Resp()]), \
                mock.patch("pypdf.PdfReader", return_value=page):
            state = fuel.run(state)
        status = self._status(state)
        self.assertTrue(status["source_failed"])
        # Dedup keys stay unwritten so the notice is retried, not banked.
        self.assertNotIn(fuel.LAST_PDF_KEY, state)
        self.assertNotIn(fuel.STATE_KEY, state)

    def test_poisoned_baseline_reseeds_silently_instead_of_pushing(self):
        # The upgrade run: real prices arrive while state still holds the old
        # parser's RD$3,380.07. Diffing would fabricate a -90% crash alert.
        pdf_bytes = b"new notice"
        state = {fuel.STATE_KEY: dict(COLLAPSE_VICTIM)}
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=COLLAPSED_TEXT))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=pdf_bytes)]), \
                mock.patch("pypdf.PdfReader", return_value=page), \
                mock.patch.object(fuel.events, "emit") as emit:
            state = fuel.run(state)
        emit.assert_not_called()
        self.assertEqual(state[fuel.STATE_KEY]["Gasolina Premium"], 343.80)
        self.assertTrue(self._status(state)["ok"])

    def test_poisoned_baseline_bypasses_the_cached_notice_shortcut(self):
        # Same URL AND same hash: without the bypass the bad prices would sit
        # in state until MICM happened to publish a new notice.
        pdf_bytes = b"same notice"
        state = {
            fuel.LAST_PDF_KEY: PDF_URL,
            fuel.LAST_PDF_HASH_KEY: fuel._hash_pdf(pdf_bytes),
            fuel.STATE_KEY: dict(COLLAPSE_VICTIM),
        }
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=COLLAPSED_TEXT))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=pdf_bytes)]), \
                mock.patch("pypdf.PdfReader", return_value=page):
            state = fuel.run(state)
        self.assertEqual(state[fuel.STATE_KEY]["Kerosene"], 335.50)

    def test_healthy_baseline_still_diffs_normally(self):
        # The self-healing check must not swallow real week-over-week moves.
        prev = dict(fuel._parse_prices(COLLAPSED_TEXT))
        prev["Gasolina Premium"] = 300.00  # a real +14.6% move to 343.80
        state = {fuel.STATE_KEY: prev}
        page = mock.Mock()
        page.pages = [mock.Mock(extract_text=mock.Mock(return_value=COLLAPSED_TEXT))]
        with mock.patch.object(fuel.requests, "get",
                               side_effect=[_Resp(text=LISTING_HTML),
                                            _Resp(content=b"new")]), \
                mock.patch("pypdf.PdfReader", return_value=page), \
                mock.patch.object(fuel.events, "emit",
                                  side_effect=lambda s, **k: s) as emit:
            state = fuel.run(state)
        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs["legacy_action"], "push")

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
