"""Tests for the price scraper (notify_watcher.topics.deals).

These pin _extract_price/extract_name against representative schema.org JSON-LD
shapes (plain Product, AggregateOffer, Shopify ProductGroup variants, @graph and
list wrappers). They exist so a silent upstream markup change is caught in CI as
a failing assertion rather than surfacing only as a "no JSON-LD price found"
line in the runner log and a missed deal alert.
"""
from __future__ import annotations

import unittest

from notify_watcher.topics import deals


def _page(*jsonld_blocks: str) -> str:
    scripts = "\n".join(
        f'<script type="application/ld+json">{b}</script>' for b in jsonld_blocks
    )
    return f"<html><head>{scripts}</head><body>ignored</body></html>"


# --- representative captured JSON-LD shapes ---------------------------------
PLAIN_PRODUCT = """
{"@context":"https://schema.org","@type":"Product","name":"Soundcore Liberty 4 NC",
 "offers":{"@type":"Offer","price":"79.99","priceCurrency":"USD"}}
"""

AGGREGATE_OFFER = """
{"@type":"Product","name":"Liberty 4 NC",
 "offers":{"@type":"AggregateOffer","lowPrice":"59.90","highPrice":"99.99","priceCurrency":"USD"}}
"""

PRODUCT_GROUP = """
{"@type":"ProductGroup","name":"Liberty 5 Pro | Earbuds with Wireless Charging",
 "hasVariant":[
   {"@type":"Product","offers":{"@type":"Offer","price":"129.99","priceCurrency":"USD"}},
   {"@type":"Product","offers":{"@type":"Offer","price":"99.99","priceCurrency":"USD"}}
 ]}
"""

GRAPH_WRAPPER = """
{"@context":"https://schema.org","@graph":[
  {"@type":"Organization","name":"Soundcore"},
  {"@type":"Product","name":"Space One","offers":{"price":"49.00","priceCurrency":"EUR"}}
]}
"""

LIST_WRAPPER = """
[{"@type":"BreadcrumbList"},
 {"@type":"Product","name":"Q45","offers":{"price":"10.00"}}]
"""

NO_PRODUCT = """
{"@context":"https://schema.org","@type":"Organization","name":"Soundcore"}
"""

COMMA_PRICE = """
{"@type":"Product","name":"Pricey","offers":{"price":"1,299.00","priceCurrency":"USD"}}
"""


class ExtractPriceTest(unittest.TestCase):
    def test_plain_product_offer(self):
        self.assertEqual(deals._extract_price(_page(PLAIN_PRODUCT)), (79.99, "USD"))

    def test_aggregate_offer_uses_low_price(self):
        self.assertEqual(deals._extract_price(_page(AGGREGATE_OFFER)), (59.90, "USD"))

    def test_product_group_picks_lowest_variant(self):
        self.assertEqual(deals._extract_price(_page(PRODUCT_GROUP)), (99.99, "USD"))

    def test_graph_wrapper(self):
        self.assertEqual(deals._extract_price(_page(GRAPH_WRAPPER)), (49.00, "EUR"))

    def test_list_wrapper_missing_currency(self):
        self.assertEqual(deals._extract_price(_page(LIST_WRAPPER)), (10.00, ""))

    def test_comma_thousands_separator(self):
        self.assertEqual(deals._extract_price(_page(COMMA_PRICE)), (1299.00, "USD"))

    def test_no_product_is_none(self):
        self.assertIsNone(deals._extract_price(_page(NO_PRODUCT)))

    def test_empty_page_is_none(self):
        self.assertIsNone(deals._extract_price("<html></html>"))

    def test_malformed_jsonld_is_skipped_not_fatal(self):
        # One broken block must not stop us reading a valid one on the same page.
        page = _page("{not valid json,,,}", PLAIN_PRODUCT)
        self.assertEqual(deals._extract_price(page), (79.99, "USD"))

    def test_lowest_across_multiple_product_nodes(self):
        page = _page(PLAIN_PRODUCT, COMMA_PRICE)  # 79.99 vs 1299.00
        self.assertEqual(deals._extract_price(page), (79.99, "USD"))


class ExtractNameTest(unittest.TestCase):
    def test_strips_marketing_tail(self):
        self.assertEqual(deals.extract_name(_page(PRODUCT_GROUP)), "Liberty 5 Pro")

    def test_plain_product_name(self):
        self.assertEqual(deals.extract_name(_page(PLAIN_PRODUCT)), "Soundcore Liberty 4 NC")

    def test_no_product_name_is_none(self):
        self.assertIsNone(deals.extract_name(_page(NO_PRODUCT)))


class FmtTest(unittest.TestCase):
    def test_with_and_without_currency(self):
        self.assertEqual(deals._fmt(79.9, "USD"), "USD 79.90")
        self.assertEqual(deals._fmt(79.9, ""), "79.90")


def _amazon_page(buybox: str, extra: str = "") -> str:
    return (
        f"<html><body>{extra}"
        f'<div id="corePrice_feature_div"><span class="a-offscreen">{buybox}</span></div>'
        f"</body></html>"
    )


class ExtractAmazonPriceTest(unittest.TestCase):
    """Amazon pages have no Product JSON-LD; the buy-box fallback reads them."""

    def test_dollar_symbol_maps_to_usd(self):
        self.assertEqual(deals._extract_amazon_price(_amazon_page("$59.99")), (59.99, "USD"))

    def test_iso_code_with_thousands_separator(self):
        # Amazon localizes the currency by viewer IP, e.g. Dominican pesos.
        self.assertEqual(
            deals._extract_amazon_price(_amazon_page("DOP3,459.57")), (3459.57, "DOP")
        )

    def test_buybox_preferred_over_other_offscreen_spans(self):
        # The struck-through list price elsewhere on the page must not win.
        page = _amazon_page(
            "$49.99", extra='<span class="a-offscreen">$99.99</span>'
        )
        self.assertEqual(deals._extract_amazon_price(page), (49.99, "USD"))

    def test_blocked_or_captcha_page_is_none(self):
        self.assertIsNone(deals._extract_amazon_price("<html><body>Robot check</body></html>"))

    def test_non_price_text_is_none(self):
        self.assertIsNone(deals._extract_amazon_price(_amazon_page("See price in cart")))


class AmazonDiagnosisTest(unittest.TestCase):
    def test_captcha_page_is_called_a_bot_wall(self):
        page = '<html><head><title>Amazon.com</title></head><body><form action="/errors/validateCaptcha"></form></body></html>'
        self.assertIn("bot wall", deals._amazon_no_price_diagnosis(page))

    def test_real_page_reports_present_markers(self):
        page = '<html><head><title>Foxtrot</title></head><body><div id="apex_desktop"></div></body></html>'
        diag = deals._amazon_no_price_diagnosis(page)
        self.assertIn("no buy-box price", diag)
        self.assertIn("#apex_desktop", diag)


class GroupNoteTest(unittest.TestCase):
    """Multi-source products (same `group`) quote each other's last price."""

    PRODUCTS = [
        {"name": "Foxtrot (Costco)", "url": "https://costco.example/p", "group": "foxtrot"},
        {"name": "Foxtrot (Amazon)", "url": "https://amazon.example/p", "group": "foxtrot"},
        {"name": "Unrelated", "url": "https://other.example/p"},
    ]

    def test_quotes_sibling_price(self):
        bucket = {"https://costco.example/p": 39.99, "https://other.example/p": 5.0}
        note = deals._group_note(self.PRODUCTS, bucket, self.PRODUCTS[1])
        self.assertEqual(note, " | Compare Foxtrot (Costco): 39.99")

    def test_sibling_without_stored_price_is_silent(self):
        note = deals._group_note(self.PRODUCTS, {}, self.PRODUCTS[1])
        self.assertEqual(note, "")

    def test_ungrouped_product_is_silent(self):
        bucket = {"https://costco.example/p": 39.99}
        note = deals._group_note(self.PRODUCTS, bucket, self.PRODUCTS[2])
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main()
