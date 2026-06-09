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


if __name__ == "__main__":
    unittest.main()
