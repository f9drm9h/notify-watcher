"""Tests for Soundcore flagship-Pro detection (notify_watcher.topics.soundcore_pro).

_is_flagship_pro is a slug allow/deny matcher tuned against the live product
sitemap; _current_slugs parses that sitemap. Both are exactly the kind of "looks
fine until the upstream slugs shift" logic that should fail loudly in CI rather
than silently start matching accessories (false alerts) or nothing (missed
releases). These pin the intended behaviour against captured slug/sitemap shapes.
"""
from __future__ import annotations

import unittest
from unittest import mock

from notify_watcher import control, events
from notify_watcher.topics import soundcore_pro as sp
from tests._util import capture_pushes

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

PAGES_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.soundcore.com/a3954-liberty-4-pro-tws-earbuds-pre-launch</loc></url>
  <url><loc>https://www.soundcore.com/de/a3954-liberty-4-pro-tws-earbuds-pre-launch-boa</loc></url>
  <url><loc>https://www.soundcore.com/liberty-3-pro</loc></url>
  <url><loc>https://www.soundcore.com/ca/collections/liberty-5-pro-care</loc></url>
  <url><loc>https://www.soundcore.com/ca/collections/liberty-5-pro-max-care</loc></url>
</urlset>
"""


class _Resp:
    def __init__(self, text: str, ok: bool = True):
        self.text = text
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise sp.requests.HTTPError("HTTP 500")


class IsFlagshipProTest(unittest.TestCase):
    def test_accepts_flagship_pro_earbuds(self):
        self.assertTrue(sp._is_flagship_pro("liberty-4-pro-earbuds"))
        self.assertTrue(sp._is_flagship_pro("liberty-5-pro-max-tws"))
        self.assertTrue(sp._is_flagship_pro("liberty-6-pro-earbuds"))
        self.assertTrue(sp._is_flagship_pro("a3954-liberty-4-pro-tws-earbuds"))

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

    def test_normalizes_current_launch_page_urls(self):
        self.assertEqual(
            sp._slug("https://www.soundcore.com/a3954-liberty-4-pro-tws-earbuds-pre-launch"),
            "a3954-liberty-4-pro-tws-earbuds",
        )
        self.assertEqual(
            sp._slug("https://www.soundcore.com/de/a3954-liberty-4-pro-tws-earbuds-pre-launch-boa"),
            "a3954-liberty-4-pro-tws-earbuds",
        )


class CurrentSlugsTest(unittest.TestCase):
    def test_parses_and_collapses_variants(self):
        slugs = sp._current_slugs(SITEMAP_XML)
        self.assertEqual(slugs, {"liberty-4-pro-earbuds", "liberty-5-pro-max-tws"})

    def test_empty_sitemap_is_empty_set(self):
        self.assertEqual(sp._current_slugs("<urlset></urlset>"), set())

    def test_parses_current_pages_sitemap_shape(self):
        slugs = sp._current_slugs(PAGES_SITEMAP_XML)
        self.assertEqual(slugs, {"a3954-liberty-4-pro-tws-earbuds"})


class FetchSitemapTest(unittest.TestCase):
    def test_falls_back_to_pages_sitemap_when_product_sitemap_fails(self):
        calls: list[str] = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if url == sp.SITEMAP_URL:
                raise sp.requests.Timeout("timeout")
            return _Resp(PAGES_SITEMAP_XML)

        with mock.patch.object(sp.requests, "get", side_effect=fake_get), \
                mock.patch.object(sp.time, "sleep"):
            self.assertEqual(sp._fetch_sitemap(), PAGES_SITEMAP_XML)

        self.assertEqual(calls, [sp.SITEMAP_URL, sp.SITEMAP_URL,
                                 sp.FALLBACK_SITEMAP_URL])

    def test_run_degrades_cleanly_when_sitemaps_fail(self):
        state = {sp.SEEN_KEY: ["liberty-4-pro-earbuds"]}
        with mock.patch.object(sp, "_fetch_sitemap",
                               side_effect=RuntimeError("blocked")):
            self.assertIs(sp.run(state), state)
        self.assertEqual(state, {sp.SEEN_KEY: ["liberty-4-pro-earbuds"]})


class DiscoveryOfferTest(unittest.TestCase):
    """run(): a discovery registers an offer, carries the [Not interested]
    button, and an ignored product is skipped on rediscovery."""

    SLUG = "liberty-6-pro-earbuds"
    URL = sp.PRODUCT_BASE + SLUG

    def _run(self, state):
        # Engine OFF (empty config sections) so the legacy push path is what
        # we assert on; the repo's real priority rules are not under test here.
        with mock.patch.object(sp, "_fetch_sitemap", return_value=""), \
                mock.patch.object(sp, "_current_slugs",
                                  return_value={"liberty-4-pro-earbuds", self.SLUG}), \
                mock.patch.object(sp, "_describe",
                                  return_value=("Liberty 6 Pro", "body")), \
                mock.patch.object(events.config, "section", return_value={}), \
                mock.patch.dict("os.environ", {"NTFY_CONTROL_TOPIC": "ctl"}), \
                capture_pushes() as sent:
            state = sp.run(state)
        return state, sent

    def _seeded(self):
        return {sp.SEEN_KEY: ["liberty-4-pro-earbuds"]}

    def test_discovery_pushes_with_ignore_button_and_tracks(self):
        state, sent = self._run(self._seeded())
        self.assertEqual(len(sent), 1)
        oid = control.offer_id("product", {"name": "Liberty 6 Pro",
                                           "url": self.URL})
        self.assertEqual(sent[0]["actions"][0]["command"], f"IGNORE:{oid}")
        self.assertEqual(state["auto_products"], [{"name": "Liberty 6 Pro",
                                                   "url": self.URL}])
        self.assertEqual(state["offers"][oid]["applied"] is not None, True)

    def test_ignored_product_is_skipped_on_rediscovery(self):
        state, _ = self._run(self._seeded())
        oid = control.offer_id("product", {"name": "Liberty 6 Pro",
                                           "url": self.URL})
        control.cmd_ignore(oid, state)
        self.assertEqual(state["auto_products"], [])  # un-tracked by the tap
        # simulate the seen-list losing the slug (e.g. state reset): the
        # ignore mark alone must keep it silent and untracked
        state[sp.SEEN_KEY] = ["liberty-4-pro-earbuds"]
        state, sent = self._run(state)
        self.assertEqual(sent, [])
        self.assertEqual(state["auto_products"], [])
        self.assertIn(self.SLUG, state[sp.SEEN_KEY])  # not retried forever


if __name__ == "__main__":
    unittest.main()
