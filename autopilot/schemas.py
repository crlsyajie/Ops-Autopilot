"""Data shapes shared by every step. The extraction model doubles as the
structured-output schema sent to Claude, so Claude can only answer in this shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

DocType = Literal["invoice", "receipt", "customer_order", "other"]
Route = Literal["auto", "review", "ignore"]


class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float
    amount: float


class Extraction(BaseModel):
    doc_type: DocType
    counterparty: str
    counterparty_email: str
    document_number: str
    document_date: str
    due_date: str
    currency: str
    line_items: list[LineItem]
    subtotal: float | None
    tax: float | None
    total: float | None
    bank_account: str
    confidence: float
    notes: str


class Issue(BaseModel):
    code: str
    message: str
    # "block": only a person may clear it. "soft": the triage agent may resolve it.
    severity: Literal["block", "soft"]


class Resolution(BaseModel):
    """What the triage agent recommends. decide.py has the final word."""

    action: Literal["auto_approve", "needs_human"]
    canonical_supplier: str
    due_date: str
    reason: str
    confidence: float


class Decision(BaseModel):
    route: Route
    reason: str
