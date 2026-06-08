"""Tests for Soundcore flagship-Pro detection (notify_watcher.topics.soundcore_pro).

_is_flagship_pro is a slug allow/deny matcher tuned against the live product
sitemap; _current_slugs parses that sitemap. Both are exactly the kind of "looks
fine until the upstream slugs shift" logic that should fail loudly in CI rather
than silently start matching accessories (false alerts) or nothing (missed
releases). These pin the intended behaviour against captured slug/sitemap shapes.
"""
from __future__ import annotations

import unittest

from notify_watcher.topics import soundcore_pro as sp

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.soundcore.com/products/liberty-4-pro-earbuds</loc></url>
  <url><loc>https://www.soundcore.com/products/liberty-4-pro-earbuds?variant=42</loc></url>
  <url><loc>https://www.soundcore.com/products/liberty-5-pro-max-tws</loc></url>
  <url><loc>https://www.soundcore.com/products/liberty-4-nc-earbuds</loc></url>
  <url><loc>https://www.soundcore.com/products/space-one</loc></url>
  <url><loc>https://www.soundcore.com/products/liberty-4-pro-replacement-tips</loc></url>
  <url><loc>https://www.soundcore.com/products/liberty-4-pro-charging-case</loc></url>
</urlset>
"""


class IsFlagshipProTest(unittest.TestCase):
    def test_accepts_flagship_pro_earbuds(self):
        self.assertTrue(sp._is_flagship_pro("liberty-4-pro-earbuds"))
        self.assertTrue(sp._is_flagship_pro("liberty-5-pro-max-tws"))
        self.assertTrue(sp._is_flagship_pro("liberty-6-pro-earbuds"))

    def test_rejects_non_pro(self):
        self.assertFalse(sp._is_flagship_pro("liberty-4-nc-earbuds"))

    def test_rejects_non_liberty(self):
        self.assertFalse(sp._is_flagship_pro("space-one"))
        self.assertFalse(sp._is_flagship_pro("soundcore-motion-x600"))

    def test_rejects_pro_without_a_product_marker(self):
        # "pro" alone isn't enough: needs earbuds/-anc/tws/pro-max/pro-anc so a
        # bare collection/landing slug doesn't match.
        self.assertFalse(sp._is_flagship_pro("liberty-6-pro"))

    def test_rejects_accessories_and_variants(self):
        for slug in (
            "liberty-4-pro-replacement-tips",
            "liberty-4-pro-charging-case",
            "liberty-4-pro-earbuds-komfort-ohrstopsel",
            "liberty-4-pro-refurbished-earbuds",
        ):
            self.assertFalse(sp._is_flagship_pro(slug), slug)


class SlugTest(unittest.TestCase):
    def test_strips_host_query_and_case(self):
        self.assertEqual(
            sp._slug("https://www.soundcore.com/products/Liberty-4-Pro-Earbuds?variant=42"),
            "liberty-4-pro-earbuds",
        )


class CurrentSlugsTest(unittest.TestCase):
    def test_parses_and_collapses_variants(self):
        slugs = sp._current_slugs(SITEMAP_XML)
        self.assertEqual(slugs, {"liberty-4-pro-earbuds", "liberty-5-pro-max-tws"})

    def test_empty_sitemap_is_empty_set(self):
        self.assertEqual(sp._current_slugs("<urlset></urlset>"), set())


if __name__ == "__main__":
    unittest.main()
