"""Tests for the grocery-deals watcher (notify_watcher.topics.groceries):
La Sirena (VTEX JSON), Nacional (Magento HTML), Bravo (promo-campaign nav)."""
from __future__ import annotations

import unittest

from notify_watcher.topics import groceries

# One intelligent-search page trimmed to the fields the parser reads.
SIRENA_PAYLOAD = {
    "products": [
        {
            "productName": "Televisor Nikkei Led 32p",
            "link": "/televisor-nikkei-32/p",
            "priceRange": {
                "sellingPrice": {"lowPrice": 8995, "highPrice": 8995},
                "listPrice": {"lowPrice": 15995, "highPrice": 15995},
            },
        },
        {
            "productName": "Pilas Duracell Aa 4 Uds",
            "link": "/pilas-duracell/p",
            "priceRange": {
                "sellingPrice": {"lowPrice": 270},
                "listPrice": {"lowPrice": 270},
            },
        },
        {  # missing price -> skipped
            "productName": "Sin Precio",
            "link": "/sin-precio/p",
            "priceRange": {"sellingPrice": {}, "listPrice": {}},
        },
    ]
}

# The structural skeleton of Nacional's Magento product item: name/link on
# a.product-item-link, machine prices under special-price / old-price.
NACIONAL_HTML = """
<ol class="product-items">
  <li class="product-item">
    <a class="product-item-link" href="https://supermercadosnacional.com/leche-1lt">
      Leche Semidescremada Parmalat 1 Lt
    </a>
    <span class="special-price"><span data-price-amount="76.95">RD$76.95</span></span>
    <span class="old-price"><span data-price-amount="85.95">RD$85.95</span></span>
  </li>
  <li class="product-item">
    <a class="product-item-link" href="https://supermercadosnacional.com/flap-meat">
      Flap Meat Certified Angus Beef, Lb
    </a>
    <span data-price-amount="649.00">RD$649.00</span>
  </li>
</ol>
"""

# Bravo's nav, duplicated like the real site (desktop + a flattened copy that
# nests the PROMOS header itself inside a submenu).
BRAVO_HTML = """
<ul id="menu-desktop">
  <li class="menu-item"><a href="https://superbravo.com.do/ofertas/">PROMOS 2026</a>
    <ul class="sub-menu">
      <li><a href="https://superbravo.com.do/base-del-concurso/">UN A&Ntilde;O DE MADRE</a></li>
      <li><a href="https://superbravo.com.do/basepromop/">DE VACAS CON PAP&Aacute;</a></li>
    </ul>
  </li>
  <li class="menu-item"><a href="https://superbravo.com.do/carnes/">CARNES</a></li>
</ul>
<ul id="menu-mobile">
  <li><a href="#">PROMOS 2026</a>
    <ul>
      <li><a href="https://superbravo.com.do/ofertas/">PROMOS 2026</a></li>
      <li><a href="https://superbravo.com.do/base-del-concurso/">UN A&Ntilde;O DE MADRE</a></li>
    </ul>
  </li>
</ul>
"""


class PctOffTest(unittest.TestCase):
    def test_real_discount(self):
        self.assertAlmostEqual(groceries._pct_off(80.0, 100.0), 20.0)

    def test_no_list_price_or_no_cut_is_zero(self):
        self.assertEqual(groceries._pct_off(100.0, None), 0.0)
        self.assertEqual(groceries._pct_off(100.0, 100.0), 0.0)
        self.assertEqual(groceries._pct_off(120.0, 100.0), 0.0)  # price rise
        self.assertEqual(groceries._pct_off(100.0, 0), 0.0)


class SeverityTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(groceries._severity(44.0, 30, 15), "high")
        self.assertEqual(groceries._severity(30.0, 30, 15), "high")
        self.assertEqual(groceries._severity(20.0, 30, 15), "moderate")
        self.assertEqual(groceries._severity(10.0, 30, 15), "low")
        self.assertEqual(groceries._severity(0.0, 30, 15), "low")


class DealKeyTest(unittest.TestCase):
    def test_same_deal_same_key(self):
        a = groceries._deal_key("Nacional", "https://x/p", 76.95)
        self.assertEqual(a, groceries._deal_key("Nacional", "https://x/p", 76.95))

    def test_deeper_cut_changes_the_key(self):
        a = groceries._deal_key("Nacional", "https://x/p", 76.95)
        b = groceries._deal_key("Nacional", "https://x/p", 69.95)
        self.assertNotEqual(a, b)

    def test_stores_do_not_collide(self):
        self.assertNotEqual(groceries._deal_key("Nacional", "https://x/p", 10.0),
                            groceries._deal_key("La Sirena", "https://x/p", 10.0))


class ParseSirenaTest(unittest.TestCase):
    def test_extracts_deals_with_discount_and_absolute_url(self):
        deals = groceries._parse_sirena(SIRENA_PAYLOAD)
        self.assertEqual(len(deals), 2)  # the priceless product is skipped
        tv = deals[0]
        self.assertEqual(tv["store"], "La Sirena")
        self.assertEqual(tv["url"], "https://www.sirena.do/televisor-nikkei-32/p")
        self.assertEqual(tv["price"], 8995.0)
        self.assertAlmostEqual(tv["pct"], 43.76, places=1)
        self.assertEqual(deals[1]["pct"], 0.0)  # list == selling

    def test_empty_payload_yields_nothing(self):
        self.assertEqual(groceries._parse_sirena({}), [])
        self.assertEqual(groceries._parse_sirena({"products": []}), [])


class ParseNacionalTest(unittest.TestCase):
    def test_only_items_with_a_real_cut_are_deals(self):
        deals = groceries._parse_nacional(NACIONAL_HTML)
        self.assertEqual(len(deals), 1)  # the single-price item is filler
        d = deals[0]
        self.assertEqual(d["name"], "Leche Semidescremada Parmalat 1 Lt")
        self.assertEqual(d["url"], "https://supermercadosnacional.com/leche-1lt")
        self.assertEqual(d["price"], 76.95)
        self.assertEqual(d["list_price"], 85.95)
        self.assertAlmostEqual(d["pct"], 10.47, places=1)

    def test_unrelated_html_yields_nothing(self):
        self.assertEqual(groceries._parse_nacional("<html><body>hola</body></html>"), [])


class ParseBravoTest(unittest.TestCase):
    def test_campaigns_found_once_without_the_menu_header(self):
        camps = groceries._parse_bravo_promos(BRAVO_HTML)
        names = sorted(c["name"] for c in camps)
        self.assertEqual(names, ["DE VACAS CON PAPÁ", "UN AÑO DE MADRE"])
        urls = {c["url"] for c in camps}
        self.assertNotIn("https://superbravo.com.do/ofertas/", urls)

    def test_nav_without_promos_menu_yields_nothing(self):
        html = '<ul><li><a href="/carnes/">CARNES</a></li></ul>'
        self.assertEqual(groceries._parse_bravo_promos(html), [])


class DealBodyTest(unittest.TestCase):
    def test_discounted_body_quotes_both_prices(self):
        body = groceries._deal_body({"price": 8995.0, "list_price": 15995.0,
                                     "pct": 43.8})
        self.assertEqual(body, "DOP 8,995.00 (was DOP 15,995.00, -44%)")

    def test_undiscounted_body_is_just_the_price(self):
        body = groceries._deal_body({"price": 270.0, "list_price": 270.0,
                                     "pct": 0.0})
        self.assertEqual(body, "DOP 270.00")


class StoreRunTest(unittest.TestCase):
    """The shared dedup pipeline, with a recording emit (no events/network).

    Keys are real ids.short hashes — the baseline normalizes entries through
    ids.normalize_seen, which would re-hash a made-up token."""

    K1 = groceries._deal_key("Nacional", "https://x/p1", 10.0)
    K2 = groceries._deal_key("Nacional", "https://x/p2", 20.0)
    S1 = groceries._deal_key("La Sirena", "https://x/p1", 10.0)

    @staticmethod
    def _emit_recorder(emitted: list):
        def _emit(state, item):
            emitted.append(item)
            return state
        return _emit

    def test_first_collection_seeds_silently(self):
        emitted: list = []
        state: dict = {}
        groceries._store_run(state, "Nacional",
                             [(self.K1, {"n": 1}), (self.K2, {"n": 2})],
                             self._emit_recorder(emitted))
        self.assertEqual(emitted, [])
        self.assertEqual(state[groceries.STATE_KEY]["Nacional"], [self.K1, self.K2])

    def test_second_run_emits_only_new_items(self):
        emitted: list = []
        state = {groceries.STATE_KEY: {"Nacional": [self.K1]}}
        groceries._store_run(state, "Nacional",
                             [(self.K1, {"n": 1}), (self.K2, {"n": 2})],
                             self._emit_recorder(emitted))
        self.assertEqual(emitted, [{"n": 2}])
        self.assertEqual(state[groceries.STATE_KEY]["Nacional"][0], self.K2)

    def test_one_stores_seed_does_not_touch_anothers_baseline(self):
        emitted: list = []
        state = {groceries.STATE_KEY: {"Nacional": [self.K1]}}
        groceries._store_run(state, "La Sirena", [(self.S1, {"n": 1})],
                             self._emit_recorder(emitted))
        self.assertEqual(emitted, [])  # La Sirena's first time: silent seed
        self.assertEqual(state[groceries.STATE_KEY]["Nacional"], [self.K1])
        self.assertEqual(state[groceries.STATE_KEY]["La Sirena"], [self.S1])


if __name__ == "__main__":
    unittest.main()
