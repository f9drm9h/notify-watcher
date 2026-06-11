"""Topic: weekly spending summary from BHD transaction emails (Gmail IMAP).

Banco BHD mails a "BHD Notificación de Transacciones" alert for each card
transaction. Every run this topic polls Gmail for unread alerts from the
configured sender, parses the transaction table out of the email HTML
(approved transactions only), appends them to ``data/spending.json`` (deduped
on date + amount + merchant), and marks each successfully processed email as
read so it is never parsed twice. On the first daily run of each ISO week
(Monday morning) it pushes a summary of the completed week: total spent in
DOP, top merchants, the biggest single expense, and a week-over-week
comparison rendered by ``changes.diff``. With no transactions recorded yet the
summary skips cleanly, so the topic is silent until the mailbox is wired up.

Gmail access is plain IMAP with an app password (``GMAIL_USER`` +
``GMAIL_APP_PASSWORD`` secrets; imaplib is stdlib, no new dependency). An MCP
connector was considered and rejected: MCP servers are session-authenticated
for AI assistants, so a headless GitHub Actions runner cannot use one — an
app password over IMAP is the equivalent capability the runner CAN hold.
Without the secrets the ingestion step logs and skips; a fetch/parse failure
never raises. Emails are fetched with BODY.PEEK and flagged ``\\Seen`` only
AFTER their transactions are safely merged and saved, so a crash mid-run
re-processes (and re-dedups) rather than losing transactions.

PRIVACY: ``data/spending.json`` holds real purchase history and is committed
back to the repo by the workflow like ``state.json``. Do not set the Gmail
secrets while the repository is public.
"""
from __future__ import annotations

import datetime as _dt
import email
import email.policy
import imaplib
import json
import logging
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from .. import changes, config, events

log = logging.getLogger(__name__)

WEEK_KEY = "spending_week_summarized"
SPENDING_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "spending.json"
DEFAULT_SENDER = "alertas@bhd.com.do"
DEFAULT_SUBJECT = "BHD Notificación de Transacciones"
IMAP_HOST = "imap.gmail.com"

# Header-name fragments (accent-stripped, lowercase) -> canonical field.
_COLUMNS = {
    "fecha": "date",
    "moneda": "currency",
    "monto": "amount",
    "comercio": "merchant",
    "estado": "status",
    "tipo": "type",
}
_DATE_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _fold(text: str) -> str:
    """Accent-stripped lowercase, for tolerant matching of Spanish labels."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _subject_matches(subject: str, wanted: str) -> bool:
    return _fold(wanted) in _fold(subject)


def _parse_amount(text: str) -> float | None:
    """``RD$1,250.00`` / ``$250.00`` / ``250.00`` -> 1250.0 / 250.0 / 250.0."""
    m = re.search(r"-?[\d,]*\.?\d+", (text or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_date(text: str) -> str:
    """Normalize the Fecha cell to ISO; an unrecognized format is kept raw
    (it still works as a dedup key, just not for week bucketing)."""
    cleaned = " ".join((text or "").split())
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(cleaned, fmt).isoformat()
        except ValueError:
            continue
    return cleaned


def _normalize_currency(text: str) -> str:
    folded = _fold(text).replace("$", "")
    if folded in ("rd", "dop", "rd peso", "pesos", "peso"):
        return "DOP"
    if folded in ("us", "usd", "dolar", "dolares"):
        return "USD"
    return (text or "").strip().upper() or "DOP"


def _parse_transactions(html: str) -> list[dict]:
    """Pure: approved transactions from a BHD alert's HTML body.

    Finds any table whose header row names a Fecha and a Monto column (matched
    accent/case-insensitively, so layout or styling changes don't matter),
    maps the remaining columns by header name, and keeps only rows whose
    Estado is "Aprobada" (a missing Estado column keeps the row — a bank table
    with no status column has nothing to filter on). Rows with no parseable
    amount are skipped.
    """
    out: list[dict] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [_fold(c.get_text()) for c in rows[0].find_all(["th", "td"])]
        idx: dict[str, int] = {}
        for i, cell in enumerate(header):
            for fragment, field in _COLUMNS.items():
                if fragment in cell and field not in idx:
                    idx[field] = i
        if "date" not in idx or "amount" not in idx:
            continue  # not the transaction table (layout/spacer tables abound)
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) <= max(idx.values()):
                continue
            status = cells[idx["status"]] if "status" in idx else "Aprobada"
            if _fold(status) != "aprobada":
                continue
            amount = _parse_amount(cells[idx["amount"]])
            if amount is None:
                continue
            out.append({
                "date": _parse_date(cells[idx["date"]]),
                "amount": amount,
                "currency": _normalize_currency(
                    cells[idx["currency"]] if "currency" in idx else "RD"),
                "merchant": cells[idx["merchant"]] if "merchant" in idx else "",
                "type": cells[idx["type"]] if "type" in idx else "",
                "source": "bhd_email",
            })
    return out


def _html_from_message(msg: email.message.Message) -> str:
    """The HTML body of an email message (multipart-aware), or ""."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")
        return ""
    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8",
                                  errors="replace")
    return ""


def _dedup_key(t: dict) -> tuple:
    return (t.get("date"), t.get("amount"), t.get("merchant"))


def _merge(existing: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """Pure: append the genuinely-new transactions. Returns (merged, added)."""
    seen = {_dedup_key(t) for t in existing}
    merged = list(existing)
    added = 0
    for t in new:
        key = _dedup_key(t)
        if key in seen:
            continue
        seen.add(key)
        merged.append(t)
        added += 1
    return merged, added


def _load_spending() -> list[dict]:
    try:
        data = json.loads(SPENDING_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        log.error("spending: data/spending.json is not valid JSON: %s", exc)
        return []
    txs = data.get("transactions") if isinstance(data, dict) else None
    return txs if isinstance(txs, list) else []


def _save_spending(transactions: list[dict]) -> None:
    SPENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPENDING_PATH.write_text(
        json.dumps({"transactions": transactions}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _ingest_emails(cfg: dict) -> int:
    """Poll Gmail over IMAP for unread BHD alerts; parse, merge, mark read.

    Returns the number of new transactions recorded. Each email is flagged
    ``\\Seen`` individually, only after the merged file is saved, so a failure
    on one message leaves the rest unread for the next run.
    """
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        log.info("spending: GMAIL_USER/GMAIL_APP_PASSWORD not set; skipping email poll")
        return 0

    sender = cfg.get("sender", DEFAULT_SENDER)
    wanted_subject = cfg.get("subject", DEFAULT_SUBJECT)
    added_total = 0
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(user, password)
        imap.select("INBOX")
        # Subject is matched in Python after decoding: IMAP SEARCH chokes on
        # non-ASCII criteria and the bank's subject carries an accent.
        status, data = imap.search(None, "UNSEEN", "FROM", f'"{sender}"')
        if status != "OK":
            log.warning("spending: IMAP search failed: %s", status)
            return 0
        for num in (data[0] or b"").split():
            # PEEK leaves the message unread until we explicitly flag it below.
            status, fetched = imap.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
            if not _subject_matches(str(msg.get("Subject", "")), wanted_subject):
                continue  # other mail from the bank; leave unread, not ours
            txs = _parse_transactions(_html_from_message(msg))
            if txs:
                merged, added = _merge(_load_spending(), txs)
                if added:
                    _save_spending(merged)
                added_total += added
            # Processed (even if it parsed to zero approved transactions):
            # mark read so it is never re-parsed.
            imap.store(num, "+FLAGS", "\\Seen")
        return added_total
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001 - a logout hiccup is irrelevant
            pass


# --- weekly summary ----------------------------------------------------------
def _iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _week_bounds(day: date, weeks_back: int = 1) -> tuple[date, date]:
    """(Monday, Sunday) of the ISO week ``weeks_back`` weeks before ``day``'s."""
    monday = day - _dt.timedelta(days=day.weekday(), weeks=weeks_back)
    return monday, monday + _dt.timedelta(days=6)


def _tx_date(t: dict) -> date | None:
    try:
        return _dt.date.fromisoformat(str(t.get("date", ""))[:10])
    except ValueError:
        return None


def _week_slice(transactions: list[dict], start: date, end: date) -> list[dict]:
    """DOP transactions dated within [start, end]."""
    out = []
    for t in transactions:
        if t.get("currency") != "DOP":
            continue
        d = _tx_date(t)
        if d and start <= d <= end:
            out.append(t)
    return out


def _summarize(transactions: list[dict], today: date) -> tuple[str, object] | None:
    """Pure. (body, Change|None) for last week's spending, or None to skip.

    None means "nothing to say": no transactions recorded at all, or none in
    the completed week (a quiet card is not worth a Monday push). The change
    is week-over-week vs the week before, omitted when that week is empty.
    """
    if not transactions:
        return None
    start, end = _week_bounds(today)
    week = _week_slice(transactions, start, end)
    if not week:
        return None

    total = sum(t["amount"] for t in week)
    by_merchant: dict[str, float] = {}
    for t in week:
        name = t.get("merchant") or "(unknown)"
        by_merchant[name] = by_merchant.get(name, 0.0) + t["amount"]
    top = sorted(by_merchant.items(), key=lambda kv: kv[1], reverse=True)[:5]
    biggest = max(week, key=lambda t: t["amount"])

    lines = [
        f"{start:%b %d}-{end:%b %d}: RD${total:,.2f} across "
        f"{len(week)} transaction{'s' if len(week) != 1 else ''}",
        "Top: " + "; ".join(f"{name} RD${amt:,.2f}" for name, amt in top),
        f"Biggest: RD${biggest['amount']:,.2f} at {biggest.get('merchant') or '(unknown)'}",
    ]

    prev = _week_slice(transactions, *_week_bounds(today, weeks_back=2))
    ch = None
    if prev:
        prev_total = sum(t["amount"] for t in prev)
        ch = changes.diff(round(prev_total, 2), round(total, 2),
                          label="Weekly spending", fmt=lambda v: f"RD${v:,.2f}")
        lines.append(f"vs prior week: {ch.summary}" if ch
                     else "vs prior week: unchanged")
    return "\n".join(lines), ch


def run(state: dict) -> dict:
    cfg = config.section("spending")
    try:
        added = _ingest_emails(cfg)
        if added:
            log.info("spending: recorded %d new transaction(s)", added)
    except Exception as exc:  # noqa: BLE001 - mail being down must not kill the run
        log.error("spending: email ingestion failed: %s", exc)

    if not os.environ.get("NOTIFY_DAILY"):
        return state  # the summary rides the daily run, like recap
    today = _dt.date.today()
    week = _iso_week(today)
    if state.get(WEEK_KEY) == week:
        return state

    result = _summarize(_load_spending(), today)
    if result is None:
        # Nothing recorded (yet): stay silent and DON'T stamp the week, so the
        # first week of real data still gets its summary even if ingestion
        # starts mid-week.
        log.info("spending: no transactions for last week; summary skipped")
        return state

    body, ch = result
    state = events.emit(
        state,
        title="Weekly spending summary",
        body=body,
        change=ch,
        topic="spending",
        severity="moderate",
        source="BHD",
        tags="credit_card",
        legacy_action="push",
        score=60,
    )
    state[WEEK_KEY] = week
    return state
