"""Tests for the weekly spending tracker (notify_watcher.topics.spending)."""
from __future__ import annotations

import email
import email.policy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from notify_watcher.topics import spending as sp

# Realistic shape of a BHD "Notificación de Transacciones" alert: a styled
# layout table the parser must ignore, then the transaction table with the
# accented Spanish headers, RD$ amounts with thousands separators, and a
# declined row that must be filtered out.
SAMPLE_HTML = """
<html><body>
<table><tr><td>Banco BHD</td></tr></table>
<table border="1">
  <tr>
    <th>Fecha</th><th>Moneda</th><th>Monto</th>
    <th>Comercio</th><th>Estado</th><th>Tipo</th>
  </tr>
  <tr>
    <td>11/06/2026 13:48</td><td>RD</td><td>$250.00</td>
    <td>CLARO RECAR</td><td>Aprobada</td><td>Compra</td>
  </tr>
  <tr>
    <td>11/06/2026 14:02</td><td>RD$</td><td>RD$1,250.50</td>
    <td>SIRENA MELLA</td><td>Aprobada</td><td>Compra</td>
  </tr>
  <tr>
    <td>11/06/2026 15:30</td><td>RD</td><td>$999.00</td>
    <td>TIENDA X</td><td>Declinada</td><td>Compra</td>
  </tr>
  <tr>
    <td>11/06/2026 16:00</td><td>US</td><td>$12.99</td>
    <td>NETFLIX.COM</td><td>Aprobada</td><td>Compra</td>
  </tr>
</table>
</body></html>
"""


class ParseTransactionsTest(unittest.TestCase):
    def test_parses_approved_rows(self):
        txs = sp._parse_transactions(SAMPLE_HTML)
        self.assertEqual(len(txs), 3)  # the Declinada row is dropped
        first = txs[0]
        self.assertEqual(first, {
            "date": "2026-06-11T13:48:00",
            "amount": 250.0,
            "currency": "DOP",
            "merchant": "CLARO RECAR",
            "type": "Compra",
            "source": "bhd_email",
        })

    def test_amounts_and_currencies_normalized(self):
        txs = sp._parse_transactions(SAMPLE_HTML)
        self.assertEqual(txs[1]["amount"], 1250.50)  # comma + RD$ prefix
        self.assertEqual(txs[1]["currency"], "DOP")
        self.assertEqual(txs[2]["currency"], "USD")

    def test_layout_tables_ignored(self):
        self.assertEqual(sp._parse_transactions("<table><tr><td>hi</td></tr></table>"), [])
        self.assertEqual(sp._parse_transactions(""), [])

    def test_header_matching_is_accent_insensitive(self):
        html = SAMPLE_HTML.replace("Fecha", "FECHA").replace("Comercio", "Comércio")
        txs = sp._parse_transactions(html)
        self.assertEqual(txs[0]["merchant"], "CLARO RECAR")


class HelpersTest(unittest.TestCase):
    def test_subject_matches_ignores_accents_and_case(self):
        self.assertTrue(sp._subject_matches(
            "BHD NOTIFICACION DE TRANSACCIONES 11/06",
            "BHD Notificación de Transacciones"))
        self.assertFalse(sp._subject_matches(
            "BHD Estado de Cuenta", "BHD Notificación de Transacciones"))

    def test_parse_amount(self):
        self.assertEqual(sp._parse_amount("RD$1,250.50"), 1250.50)
        self.assertEqual(sp._parse_amount("$250.00"), 250.0)
        self.assertIsNone(sp._parse_amount("n/a"))

    def test_parse_date_formats(self):
        self.assertEqual(sp._parse_date("11/06/2026 13:48"), "2026-06-11T13:48:00")
        self.assertEqual(sp._parse_date("11/06/2026"), "2026-06-11T00:00:00")
        self.assertEqual(sp._parse_date("whenever"), "whenever")  # raw, not raised

    def test_html_from_multipart_message(self):
        raw = (
            "From: alertas@bhd.com.do\nSubject: test\nMIME-Version: 1.0\n"
            'Content-Type: multipart/alternative; boundary="B"\n\n'
            "--B\nContent-Type: text/plain\n\nplain\n"
            "--B\nContent-Type: text/html\n\n<p>rich</p>\n--B--\n"
        )
        msg = email.message_from_string(raw, policy=email.policy.default)
        self.assertIn("<p>rich</p>", sp._html_from_message(msg))


class MergeTest(unittest.TestCase):
    TX = {"date": "2026-06-11T13:48:00", "amount": 250.0, "currency": "DOP",
          "merchant": "CLARO RECAR", "type": "Compra", "source": "bhd_email"}

    def test_duplicate_dropped_on_date_amount_merchant(self):
        merged, added = sp._merge([self.TX], [dict(self.TX, type="Otro")])
        self.assertEqual((len(merged), added), (1, 0))

    def test_new_transaction_appended(self):
        other = dict(self.TX, amount=300.0)
        merged, added = sp._merge([self.TX], [other, self.TX])
        self.assertEqual((len(merged), added), (2, 1))


class SummarizeTest(unittest.TestCase):
    # Monday 2026-06-08: "last week" is Jun 1 (Mon) .. Jun 7 (Sun).
    TODAY = date(2026, 6, 8)

    @staticmethod
    def _tx(day, amount, merchant, currency="DOP"):
        return {"date": f"{day}T12:00:00", "amount": amount,
                "currency": currency, "merchant": merchant,
                "type": "Compra", "source": "bhd_email"}

    def test_no_transactions_skips(self):
        self.assertIsNone(sp._summarize([], self.TODAY))

    def test_quiet_week_skips(self):
        old = [self._tx("2026-05-01", 100, "X")]
        self.assertIsNone(sp._summarize(old, self.TODAY))

    def test_summary_totals_top_and_biggest(self):
        txs = [
            self._tx("2026-06-01", 1250.50, "SIRENA MELLA"),
            self._tx("2026-06-03", 250.00, "CLARO RECAR"),
            self._tx("2026-06-05", 300.00, "SIRENA MELLA"),
            self._tx("2026-06-06", 12.99, "NETFLIX.COM", currency="USD"),  # not DOP
            self._tx("2026-06-08", 999.00, "THIS WEEK"),  # outside the window
        ]
        body, ch = sp._summarize(txs, self.TODAY)
        self.assertIn("RD$1,800.50 across 3 transactions", body)
        self.assertIn("SIRENA MELLA RD$1,550.50", body)
        self.assertIn("Biggest: RD$1,250.50 at SIRENA MELLA", body)
        self.assertIsNone(ch)  # no prior-week data -> no comparison line
        self.assertNotIn("vs prior week", body)

    def test_week_over_week_change(self):
        txs = [
            self._tx("2026-05-26", 1000.00, "A"),  # prior week (May 25-31)
            self._tx("2026-06-02", 1500.00, "B"),  # last week
        ]
        body, ch = sp._summarize(txs, self.TODAY)
        self.assertIsNotNone(ch)
        self.assertIn("vs prior week", body)
        self.assertEqual(ch.metadata["abs_delta"], 500.0)

    def test_flat_week_renders_unchanged(self):
        txs = [
            self._tx("2026-05-26", 500.00, "A"),
            self._tx("2026-06-02", 500.00, "B"),
        ]
        body, ch = sp._summarize(txs, self.TODAY)
        self.assertIsNone(ch)  # changes.diff returns None on equal values
        self.assertIn("vs prior week: unchanged", body)


class EncryptedStorageTest(unittest.TestCase):
    """data/spending.json.enc is ciphertext at rest, keyed by SPENDING_KEY."""

    TX = {"date": "2026-06-11T13:48:00", "amount": 250.0, "currency": "DOP",
          "merchant": "CLARO RECAR", "type": "Compra", "source": "bhd_email"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "spending.json.enc"
        path_patch = mock.patch.object(sp, "SPENDING_PATH", self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        self.key = Fernet.generate_key().decode()
        env_patch = mock.patch.dict("os.environ", {"SPENDING_KEY": self.key})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_round_trip_and_ciphertext_at_rest(self):
        sp._save_spending([self.TX])
        self.assertEqual(sp._load_spending(), [self.TX])
        raw = self.path.read_bytes()
        # Fernet token, not JSON: the version-byte prefix, and no plaintext.
        # (Don't assert on short substrings like b"250" — base64 ciphertext
        # can contain any 3-character run by chance.)
        self.assertTrue(raw.startswith(b"gAAAA"))
        self.assertNotIn(b"CLARO", raw)
        self.assertNotIn(b"transactions", raw)

    def test_missing_file_is_empty_without_needing_a_key(self):
        with mock.patch.dict("os.environ", {"SPENDING_KEY": ""}):
            self.assertEqual(sp._load_spending(), [])

    def test_missing_key_blocks_save(self):
        with mock.patch.dict("os.environ", {"SPENDING_KEY": ""}):
            with self.assertRaises(sp.SpendingLocked):
                sp._save_spending([self.TX])

    def test_wrong_key_raises_instead_of_wiping(self):
        sp._save_spending([self.TX])
        other = Fernet.generate_key().decode()
        with mock.patch.dict("os.environ", {"SPENDING_KEY": other}):
            with self.assertRaises(sp.SpendingLocked):
                sp._load_spending()
        # The original ciphertext is untouched and still readable.
        self.assertEqual(sp._load_spending(), [self.TX])

    def test_malformed_key_raises(self):
        with mock.patch.dict("os.environ", {"SPENDING_KEY": "not-a-key"}):
            with self.assertRaises(sp.SpendingLocked):
                sp._save_spending([self.TX])


class WeekBoundsTest(unittest.TestCase):
    def test_last_week_from_monday(self):
        self.assertEqual(sp._week_bounds(date(2026, 6, 8)),
                         (date(2026, 6, 1), date(2026, 6, 7)))

    def test_last_week_from_midweek(self):
        self.assertEqual(sp._week_bounds(date(2026, 6, 11)),
                         (date(2026, 6, 1), date(2026, 6, 7)))

    def test_two_weeks_back(self):
        self.assertEqual(sp._week_bounds(date(2026, 6, 8), weeks_back=2),
                         (date(2026, 5, 25), date(2026, 5, 31)))


if __name__ == "__main__":
    unittest.main()
