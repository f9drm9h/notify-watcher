"""Tests for the INDOMET severe-weather alert watcher (notify_watcher.topics.onamet)."""
from __future__ import annotations

import datetime as dt
import unittest

from notify_watcher.topics import onamet

CAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <cap:identifier>urn:oid:2.49.0.1.214.0.2026.6.10.16.1.53</cap:identifier>
  <cap:sender>cmorales@indomet.gov.do</cap:sender>
  <cap:sent>2026-06-10T12:01:53-04:00</cap:sent>
  <cap:status>Actual</cap:status>
  <cap:msgType>Alert</cap:msgType>
  <cap:scope>Public</cap:scope>
  <cap:info>
    <cap:language>es</cap:language>
    <cap:category>Met</cap:category>
    <cap:event>inundaciones repentinas</cap:event>
    <cap:urgency>Expected</cap:urgency>
    <cap:severity>Moderate</cap:severity>
    <cap:certainty>Likely</cap:certainty>
    <cap:onset>2026-06-10T12:06:00-04:00</cap:onset>
    <cap:expires>2026-06-12T11:58:12-04:00</cap:expires>
    <cap:headline>ALERTA meteorologica ante el riesgo de inundaciones</cap:headline>
    <cap:description>Acumulados de precipitaciones entre 50 y 75 mm.</cap:description>
    <cap:area>
      <cap:areaDesc>Santiago</cap:areaDesc>
      <cap:polygon>19.0,-71.0 19.1,-71.1 19.0,-71.0</cap:polygon>
    </cap:area>
    <cap:area>
      <cap:areaDesc>Azua</cap:areaDesc>
    </cap:area>
  </cap:info>
</cap:alert>"""


class SeverityTest(unittest.TestCase):
    def test_aviso_is_critical(self):
        self.assertEqual(onamet._severity("AVISO meteorológico por onda tropical"), "critical")

    def test_alerta_is_high(self):
        self.assertEqual(onamet._severity("ALERTA meteorológica por vaguada"), "high")

    def test_cap_extreme_backstops_critical(self):
        self.assertEqual(onamet._severity("Boletín especial", "Extreme"), "critical")

    def test_cap_severe_backstops_high(self):
        self.assertEqual(onamet._severity("Boletín especial", "Severe"), "high")

    def test_plain_bulletin_is_moderate(self):
        self.assertEqual(onamet._severity("Informe del tiempo", "Moderate"), "moderate")


class ContentKeyTest(unittest.TestCase):
    def test_whitespace_and_case_do_not_change_the_key(self):
        a = onamet._content_key("ALERTA por  lluvias", "Acumulados de 50 mm.")
        b = onamet._content_key("alerta por lluvias", "Acumulados de 50  mm.")
        self.assertEqual(a, b)

    def test_different_alerts_get_different_keys(self):
        a = onamet._content_key("ALERTA por lluvias", "50 mm")
        b = onamet._content_key("AVISO por crecidas", "75 mm")
        self.assertNotEqual(a, b)


class ParseCapTest(unittest.TestCase):
    def test_extracts_fields_and_areas(self):
        cap = onamet._parse_cap(CAP_XML)
        self.assertEqual(cap["event"], "inundaciones repentinas")
        self.assertEqual(cap["severity"], "Moderate")
        self.assertEqual(cap["expires"], "2026-06-12T11:58:12-04:00")
        self.assertIn("precipitaciones", cap["description"])
        self.assertEqual(cap["areas"], ["Santiago", "Azua"])

    def test_malformed_xml_returns_empty(self):
        self.assertEqual(onamet._parse_cap("<not-cap>"), {})
        self.assertEqual(onamet._parse_cap("plain text"), {})


class ExpiryTest(unittest.TestCase):
    NOW = dt.datetime(2026, 6, 11, 12, 0, tzinfo=dt.timezone.utc)

    def test_future_expiry_is_active(self):
        self.assertFalse(onamet._is_expired("2026-06-12T11:58:12-04:00", self.NOW))

    def test_past_expiry_is_expired(self):
        self.assertTrue(onamet._is_expired("2026-06-10T11:58:12-04:00", self.NOW))

    def test_naive_timestamp_is_treated_as_utc(self):
        self.assertFalse(onamet._is_expired("2026-06-12T00:00:00", self.NOW))

    def test_garbage_counts_as_expired(self):
        self.assertTrue(onamet._is_expired("not-a-date", self.NOW))
        self.assertTrue(onamet._is_expired("", self.NOW))

    def test_prune_drops_only_expired_keys(self):
        active = {"old": "2026-06-01T00:00:00+00:00", "live": "2026-06-20T00:00:00+00:00"}
        self.assertEqual(onamet._prune_active(active, self.NOW), {"live": "2026-06-20T00:00:00+00:00"})


class FormatAreasTest(unittest.TestCase):
    def test_short_list_is_joined(self):
        self.assertEqual(onamet._fmt_areas(["Santiago", "Azua"]), "Santiago, Azua")

    def test_long_list_is_truncated_with_count(self):
        areas = [f"Provincia {i}" for i in range(9)]
        self.assertEqual(
            onamet._fmt_areas(areas, limit=6),
            "Provincia 0, Provincia 1, Provincia 2, Provincia 3, Provincia 4, Provincia 5 (+3 more)",
        )


if __name__ == "__main__":
    unittest.main()
