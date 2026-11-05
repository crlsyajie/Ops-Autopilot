"""Step 2, Validate: plain Python rules, no AI. Models are good at reading and
bad at arithmetic and policy, so every number and every limit is checked here.

"block" issues can only be cleared by a person. "soft" issues can be cleared
by the triage agent, but only by supplying data this module then re-checks."""

from __future__ import annotations

import re
from datetime import date

from .config import Policy
from .schemas import Extraction, Issue
from .store import Store

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONEY_TYPES = ("invoice", "receipt", "customer_order")


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def find_supplier(name: str, suppliers: list[dict]) -> dict | None:
    """Exact match on the name or a listed alias, ignoring case and punctuation."""
    key = normalize(name)
    if not key:
        return None
    for s in suppliers:
        if key in {normalize(n) for n in [s["name"], *s.get("aliases", [])]}:
            return s
    return None


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def valid_iso(value: str) -> bool:
    if not ISO_DATE.match(value or ""):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def validate(ext: Extraction, policy: Policy, suppliers: list[dict], store: Store, doc_id: str) -> list[Issue]:
    issues: list[Issue] = []

    def add(code: str, severity: str, message: str) -> None:
        issues.append(Issue(code=code, severity=severity, message=message))

    if ext.doc_type == "other":
        return issues

    if ext.confidence < policy.min_confidence:
        add("low_confidence", "block", f"The model is only {ext.confidence:.0%} sure of this reading.")

    if not ext.counterparty.strip():
        add("missing_counterparty", "block", "No supplier or customer name was found.")

    if ext.total is None:
        add("missing_total", "block", "No total amount was found.")
    else:
        if ext.total > policy.auto_approve_limit:
            add("over_limit", "block",
                f"Total {ext.total:,.2f} is above the auto-approve limit of {policy.auto_approve_limit:,.2f}.")
        if ext.currency and ext.currency != policy.home_currency:
            add("foreign_currency", "block", f"Billed in {ext.currency}; a person must confirm the exchange rate.")

    # Arithmetic: each line, the lines against the subtotal or total, and subtotal + tax against the total.
    tol = policy.total_tolerance
    for item in ext.line_items:
        if abs(item.quantity * item.unit_price - item.amount) > tol:
            add("line_math", "block",
                f"'{item.description}': {item.quantity:g} x {item.unit_price:,.2f} is not {item.amount:,.2f}.")
    if ext.line_items and ext.total is not None:
        lines = sum(i.amount for i in ext.line_items)
        targets = [t for t in (ext.subtotal, ext.total) if t is not None]
        if not any(abs(lines - t) <= tol for t in targets):
            add("total_mismatch", "block",
                f"The lines add up to {lines:,.2f}, which matches neither the subtotal nor the total ({ext.total:,.2f}).")
    if ext.subtotal is not None and ext.tax is not None and ext.total is not None:
        if abs(ext.subtotal + ext.tax - ext.total) > tol:
            add("tax_mismatch", "block",
                f"Subtotal {ext.subtotal:,.2f} + tax {ext.tax:,.2f} is not the total {ext.total:,.2f}.")

    for label, value in (("document date", ext.document_date), ("due date", ext.due_date)):
        if value and not valid_iso(value):
            add("bad_date", "block", f"The {label} '{value}' is not a valid date.")

    if ext.doc_type in ("invoice", "receipt"):
        supplier = find_supplier(ext.counterparty, suppliers)
        if ext.counterparty.strip() and supplier is None and policy.require_known_supplier:
            add("unknown_supplier", "soft", f"'{ext.counterparty}' is not in the supplier list.")
        if supplier and ext.bank_account and digits(ext.bank_account)[-6:] != digits(supplier.get("bank_account", ""))[-6:]:
            add("bank_details_changed", "block",
                "The payment account differs from the one on file. Confirm with the supplier by phone before paying.")

    if ext.doc_type == "invoice":
        if not ext.document_number.strip():
            add("missing_document_number", "block", "The invoice has no number, so duplicates cannot be checked.")
        else:
            supplier = find_supplier(ext.counterparty, suppliers)
            name = supplier["name"] if supplier else ext.counterparty
            dupes = store.find_duplicates(name, ext.document_number, doc_id)
            if dupes:
                add("duplicate", "block",
                    f"Invoice {ext.document_number} from {name} was already received ({dupes[0]['filename']}).")
        if not ext.due_date:
            add("missing_due_date", "soft", "The invoice has no due date.")
        elif ext.document_date and valid_iso(ext.due_date) and valid_iso(ext.document_date) and ext.due_date < ext.document_date:
            add("bad_date", "block", "The due date is before the invoice date.")

    return issues
