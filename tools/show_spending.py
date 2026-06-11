"""Decrypt and pretty-print the spending log locally.

The committed ``data/spending.json.enc`` is Fernet ciphertext; this is the
only way to read it outside the Actions runner. The key is looked up in the
``SPENDING_KEY`` env var first, then in ``.secrets/spending.key`` next to the
repo root (that file is gitignored — it must never be committed).

Usage (from the repo root):
    python tools/show_spending.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent.parent
ENC_PATH = ROOT / "data" / "spending.json.enc"
KEY_PATH = ROOT / ".secrets" / "spending.key"


def _key() -> str:
    key = os.environ.get("SPENDING_KEY", "").strip()
    if key:
        return key
    try:
        return KEY_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        sys.exit("No key: set SPENDING_KEY or put the key in .secrets/spending.key")


def main() -> None:
    try:
        token = ENC_PATH.read_bytes()
    except FileNotFoundError:
        sys.exit("data/spending.json.enc does not exist yet — no transactions recorded.")
    try:
        data = json.loads(Fernet(_key().encode("ascii")).decrypt(token))
    except (ValueError, InvalidToken):
        sys.exit("Decryption failed: the key does not match data/spending.json.enc.")

    txs = data.get("transactions", [])
    if not txs:
        print("No transactions recorded yet.")
        return
    for t in txs:
        print(f"{t.get('date', '?'):<20} {t.get('currency', ''):>3} "
              f"{t.get('amount', 0):>12,.2f}  {t.get('merchant', '')}")
    total = sum(t.get("amount", 0) for t in txs if t.get("currency") == "DOP")
    print(f"\n{len(txs)} transaction(s); DOP total RD${total:,.2f}")


if __name__ == "__main__":
    main()
