"""Step 5, Act: the side effects. Each one is safe to run twice (keyed by
document id), so a retry never records the same invoice twice.

- invoice         -> payables ledger
- receipt         -> expenses ledger
- customer_order  -> orders ledger + a reply DRAFT (never sent automatically)
Every ledger row is also posted to a Google Sheets webhook when one is set.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .schemas import Extraction
from .store import now

LEDGERS = {"invoice": "payables.csv", "receipt": "expenses.csv", "customer_order": "orders.csv"}
COLUMNS = ["doc_id", "recorded_at", "counterparty", "document_number", "document_date", "due_date",
           "currency", "total", "items", "source_file", "approved_by"]


def post_json(url: str, payload: dict, timeout: float = 10.0) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL comes from our own config)
        resp.read()


def _already_recorded(path: Path, doc_id: str) -> bool:
    if not path.exists():
        return False
    with path.open(newline="") as f:
        return any(row.get("doc_id") == doc_id for row in csv.DictReader(f))


def record(settings: Settings, doc_id: str, filename: str, ext: Extraction, approved_by: str,
           log: Callable[[str, Any], None]) -> list[str]:
    done: list[str] = []
    ledger = LEDGERS.get(ext.doc_type)
    if not ledger:
        return done
    path = settings.out_dir / ledger
    row = {
        "doc_id": doc_id, "recorded_at": now(), "counterparty": ext.counterparty,
        "document_number": ext.document_number, "document_date": ext.document_date, "due_date": ext.due_date,
        "currency": ext.currency, "total": ext.total,
        "items": "; ".join(f"{i.quantity:g} x {i.description}" for i in ext.line_items),
        "source_file": filename, "approved_by": approved_by,
    }
    if _already_recorded(path, doc_id):
        log("action_skip", {"ledger": ledger, "reason": "already recorded"})
    else:
        new = not path.exists()
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                writer.writeheader()
            writer.writerow(row)
        done.append(f"recorded in {ledger}")
        log("action_ledger", {"ledger": ledger, "row": row})

    if settings.sheets_webhook_url:
        try:
            post_json(settings.sheets_webhook_url, {"sheet": ledger.removesuffix(".csv"), "row": row})
            done.append("sent to Google Sheets")
            log("action_sheets", {"sheet": ledger})
        except Exception as e:  # the ledger file is the source of truth; the sheet is a mirror
            log("action_sheets_failed", {"error": str(e)})

    if ext.doc_type == "customer_order" and ext.counterparty_email:
        drafts = settings.out_dir / "reply_drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        items = "\n".join(f"  - {i.quantity:g} x {i.description}: {ext.currency} {i.amount:,.2f}" for i in ext.line_items)
        body = (
            f"To: {ext.counterparty_email}\nSubject: Your order with Kapehan sa Kanto\n\n"
            f"Hi {ext.counterparty.split()[0] if ext.counterparty else 'there'},\n\n"
            f"Thank you for your order. Here is what we have:\n{items}\n"
            f"  Total: {ext.currency} {ext.total:,.2f}\n\n"
            "We will confirm the pickup or delivery time shortly.\n\nSalamat!\nKapehan sa Kanto\n"
        )
        (drafts / f"{doc_id}.txt").write_text(body)
        done.append("reply drafted (not sent)")
        log("action_reply_draft", {"to": ext.counterparty_email})
    return done


def notify_review(settings: Settings, doc_id: str, filename: str, reason: str, log: Callable[[str, Any], None]) -> None:
    link = f"{settings.public_base_url}/documents/{doc_id}"
    if not settings.slack_webhook_url:
        log("notify_skipped", {"reason": "SLACK_WEBHOOK_URL not set", "link": link})
        return
    text = f":mag: *Needs review:* {filename}\n{reason}\n<{link}|Open in Ops Autopilot>"
    try:
        post_json(settings.slack_webhook_url, {"text": text})
        log("notify_slack", {"link": link})
    except Exception as e:
        log("notify_failed", {"error": str(e)})
