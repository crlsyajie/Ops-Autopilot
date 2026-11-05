"""Step 4, Decide: the routing policy, in code, in one place.

    other document            -> ignore (nothing is recorded)
    any open issue            -> review (a person decides)
    clean and confident       -> auto   (actions run without a person)
"""

from __future__ import annotations

from .schemas import Decision, Extraction, Issue, Resolution
from .validate import find_supplier, valid_iso


def apply_resolution(ext: Extraction, resolution: Resolution, suppliers: list[dict], min_confidence: float) -> list[str]:
    """Apply what the agent proposed, but only facts we can verify. Returns what changed."""
    changes: list[str] = []
    if resolution.action != "auto_approve" or resolution.confidence < min_confidence:
        return changes
    supplier = find_supplier(resolution.canonical_supplier, suppliers)
    if supplier and supplier["name"] != ext.counterparty:
        changes.append(f"counterparty: '{ext.counterparty}' -> '{supplier['name']}'")
        ext.notes = (ext.notes + f" Written on the document as '{ext.counterparty}'.").strip()
        ext.counterparty = supplier["name"]
    if not ext.due_date and valid_iso(resolution.due_date):
        if not ext.document_date or resolution.due_date >= ext.document_date:
            changes.append(f"due_date: '' -> '{resolution.due_date}'")
            ext.due_date = resolution.due_date
    return changes


def decide(ext: Extraction, issues: list[Issue]) -> Decision:
    if ext.doc_type == "other":
        return Decision(route="ignore", reason="Not a business document.")
    if issues:
        return Decision(route="review", reason=" ".join(i.message for i in issues))
    return Decision(route="auto", reason="All checks passed.")
