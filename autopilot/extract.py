"""Step 1, Extract: one Claude call that turns a messy document into the
Extraction schema. Structured outputs guarantee the shape; validate.py checks
whether the content makes sense."""

from __future__ import annotations

from typing import Any

from .config import Settings
from .llm import LIVE_EXTRAS, CostMeter
from .loaders import LoadedDoc
from .schemas import Extraction

SYSTEM = """You read incoming business documents for Kapehan sa Kanto, a small café in Quezon City, Philippines.
Documents arrive as emails, PDFs, photos of paper receipts and chat messages, in English, Filipino or Taglish.

Classify the document and pull out its fields:
- invoice: a supplier is billing the café and expects payment later.
- receipt: proof the café already paid for something.
- customer_order: a customer wants to buy from the café (catering, bulk coffee, pastries).
- other: anything else (newsletters, spam, personal messages). Leave the money fields empty.

counterparty is the supplier for invoices and receipts, and the customer for customer orders.
Copy names exactly as written; do not correct or expand them.

Formats:
- Dates as YYYY-MM-DD. Use an empty string when a date is not in the document.
- Amounts as plain numbers without currency symbols or thousands separators.
- currency as an ISO 4217 code. "₱", "PHP", "Php" and "P" before an amount all mean PHP.
- line_items: one entry per billed line. If a line has no quantity, use 1.
- subtotal, tax, total: null when the document does not state them. Never compute a missing total yourself.
- bank_account: the bank name and account number the document asks payment to go to, exactly as written. Empty string if none.
- confidence: from 0 to 1, how sure you are that every field is right. Lower it for blurry, partial or ambiguous documents.
- notes: anything a bookkeeper should know (handwritten corrections, partial payments, unclear amounts). Empty string if nothing.

Never invent values that are not in the document."""


class ExtractionFailed(Exception):
    pass


def extract(client: Any, settings: Settings, doc: LoadedDoc, meter: CostMeter, live: bool) -> Extraction:
    extras = LIVE_EXTRAS if live else {}
    response = client.beta.messages.parse(
        model=settings.model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [*doc.blocks, {"type": "text", "text": f"Source file: {doc.filename}\nExtract the fields from this document."}],
        }],
        output_format=Extraction,
        output_config={"effort": settings.extract_effort},
        **extras,
    )
    meter.add(response, settings.model)

    if response.stop_reason == "refusal":
        raise ExtractionFailed("The model declined to read this document.")
    if response.stop_reason == "max_tokens":
        raise ExtractionFailed("The answer was cut off at max_tokens.")
    extraction = response.parsed_output
    if extraction is None:
        raise ExtractionFailed("The model returned no structured output.")

    extraction.confidence = max(0.0, min(1.0, extraction.confidence))
    extraction.currency = (extraction.currency or "").upper()
    return extraction
