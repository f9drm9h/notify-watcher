"""One-off local diagnostic: find the real BHD alert sender and dry-run the parser.

Read-only (readonly select + BODY.PEEK); prints From/Subject/Date of recent
candidate emails and what _parse_transactions extracts from them. Never saves.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from notify_watcher.topics import spending as sp  # noqa: E402

user = os.environ["GMAIL_USER"]
password = os.environ["GMAIL_APP_PASSWORD"]

imap = imaplib.IMAP4_SSL("imap.gmail.com")
imap.login(user, password)
imap.select("INBOX", readonly=True)

for label, criteria in [
    ("FROM bhd.com.do", ("SINCE", "05-Jun-2026", "FROM", '"bhd.com.do"')),
    ("FROM bhd", ("SINCE", "05-Jun-2026", "FROM", '"bhd"')),
    ("SUBJECT Transacciones", ("SINCE", "05-Jun-2026", "SUBJECT", '"Transacciones"')),
]:
    status, data = imap.search(None, *criteria)
    nums = (data[0] or b"").split()
    print(f"[{label}] -> {status}, {len(nums)} message(s)")
    for num in nums[-5:]:
        status, fetched = imap.fetch(num, "(BODY.PEEK[])")
        if status != "OK" or not fetched or not fetched[0]:
            continue
        msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
        print(f"  From: {msg.get('From')}")
        print(f"  Subject: {msg.get('Subject')}")
        print(f"  Date: {msg.get('Date')}")
        txs = sp._parse_transactions(sp._html_from_message(msg))
        print(f"  parsed: {txs}")
    print()

imap.logout()
