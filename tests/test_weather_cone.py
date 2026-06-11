"""Tests for the forecast-cone URL derivation (notify_watcher.topics.weather).

Mock entry text mirrors the real NHC Atlantic feed during Leslie/Milton
(Oct 2024): summary items title "(AT3/AL132024)", advisory bodies carrying the
ATCF id, and per-storm Graphics items embedding the cone PNG verbatim. Pure —
no network.
"""
from __future__ import annotations

import unittest

from notify_watcher.topics import weather

CONE_LESLIE = ("https://www.nhc.noaa.gov/storm_graphics/AT13/"
               "AL132024_5day_cone_with_line_and_wind_sm2.png")


class ConeUrlTest(unittest.TestCase):
    def test_embedded_cone_url_is_used_verbatim(self):
        summary = ('<img src="https://www.nhc.noaa.gov/storm_graphics/AT13/'
                   'AL132024_5day_cone_with_line_and_wind_sm2.png" alt="cone">')
        self.assertEqual(weather._cone_url("Tropical Storm Leslie Graphics",
                                           summary, ""), CONE_LESLIE)

    def test_embedded_url_beats_derivation(self):
        # A Graphics entry names both the ATCF id and the image; the verbatim
        # URL must win so we never substitute a guessed filename for a real one.
        summary = f"AL132024 cone: {CONE_LESLIE}"
        self.assertEqual(weather._cone_url("t", summary, ""), CONE_LESLIE)

    def test_derived_from_summary_item_title(self):
        url = weather._cone_url(
            "Summary for Tropical Storm Leslie (AT3/AL132024)",
            "...LESLIE NOW A TROPICAL STORM...", "")
        self.assertEqual(url, CONE_LESLIE)

    def test_derived_from_public_advisory_body(self):
        # The ATCF id appears in the advisory text, not the title or link.
        summary = ("BULLETIN\nHurricane Milton Advisory Number 13\n"
                   "NWS National Hurricane Center Miami FL       AL142024\n"
                   "...MILTON A CATEGORY 5 HURRICANE...")
        url = weather._cone_url(
            "Hurricane Milton Public Advisory Number 13A", summary,
            "https://www.nhc.noaa.gov/text/refresh/MIATCPAT4+shtml/081151.shtml")
        self.assertEqual(url, ("https://www.nhc.noaa.gov/storm_graphics/AT14/"
                               "AL142024_5day_cone_with_line_and_wind_sm2.png"))

    def test_lowercase_atcf_id_is_normalized(self):
        url = weather._cone_url("", "advisory al012026 issued", "")
        self.assertEqual(url, ("https://www.nhc.noaa.gov/storm_graphics/AT01/"
                               "AL012026_5day_cone_with_line_and_wind_sm2.png"))

    def test_wallet_id_alone_is_not_mistaken_for_a_storm_id(self):
        # Local statements carry only the AT3 wallet (in text and the TCPAT3
        # product link); the wallet can't locate the graphics directory.
        url = weather._cone_url(
            "Local Statement for Tampa Bay Area, FL",
            "Issued by NWS for wallet AT4",
            "https://www.nhc.noaa.gov/text/refresh/MIATCPAT4+shtml/080835.shtml")
        self.assertIsNone(url)

    def test_no_storm_entries_yield_none(self):
        self.assertIsNone(weather._cone_url(
            "There are no tropical cyclones at this time.",
            "No tropical cyclones as of Thu, 11 Jun 2026 00:03:26 GMT",
            "https://www.nhc.noaa.gov/"))
        self.assertIsNone(weather._cone_url(
            "Atlantic Tropical Weather Outlook",
            "Formation chance through 48 hours...low...10 percent.",
            "https://www.nhc.noaa.gov/gtwo.php?basin=atlc"))

    def test_empty_and_none_inputs_are_safe(self):
        self.assertIsNone(weather._cone_url("", "", ""))
        self.assertIsNone(weather._cone_url(None, None, None))

    def test_non_cone_storm_graphics_are_not_matched_verbatim(self):
        # Wind-probability / surge PNGs live in the same directory; only the
        # cone should be attached. The ATCF id still derives the cone URL.
        summary = ("see https://www.nhc.noaa.gov/storm_graphics/AT13/"
                   "AL132024_wind_probs_34_F120_sm2.png")
        self.assertEqual(weather._cone_url("", summary, ""), CONE_LESLIE)


if __name__ == "__main__":
    unittest.main()
