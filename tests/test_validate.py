from autopilot.config import Policy
from autopilot.schemas import Extraction, LineItem
from autopilot.store import Store
from autopilot.validate import find_supplier, validate

SUPPLIERS = [{"name": "Bukid Coffee Beans Trading", "aliases": ["Bukid Coffee"], "payment_terms_days": 30,
              "bank_account": "BPI 1234-5678-90"}]


def make(**kw) -> Extraction:
    base = dict(doc_type="invoice", counterparty="Bukid Coffee Beans Trading", counterparty_email="",
                document_number="INV-1", document_date="2026-10-01", due_date="2026-10-31", currency="PHP",
                line_items=[LineItem(description="Beans", quantity=2, unit_price=500, amount=1000)],
                subtotal=None, tax=None, total=1000, bank_account="BPI 1234-5678-90", confidence=0.95, notes="")
    base.update(kw)
    return Extraction(**base)


def codes(ext, store=None):
    return {i.code for i in validate(ext, Policy(), SUPPLIERS, store or Store(":memory:"), "doc")}


def test_clean_invoice_has_no_issues():
    assert codes(make()) == set()


def test_alias_matches_supplier():
    assert find_supplier("bukid coffee", SUPPLIERS)["name"] == "Bukid Coffee Beans Trading"
    assert find_supplier("Bukid Coffee Beans Trading Inc", SUPPLIERS) is None


def test_math_errors_are_blocking():
    bad_line = make(line_items=[LineItem(description="Beans", quantity=2, unit_price=500, amount=900)], total=900)
    assert "line_math" in codes(bad_line)
    assert "total_mismatch" in codes(make(total=1200))
    assert "tax_mismatch" in codes(make(subtotal=1000, tax=120, total=1000))


def test_vat_inclusive_receipt_passes():
    ext = make(doc_type="receipt", subtotal=892.86, tax=107.14, total=1000, document_number="",
               line_items=[LineItem(description="Beans", quantity=2, unit_price=500, amount=1000)])
    assert codes(ext) == set()


def test_limits_currency_and_confidence():
    big = make(total=60000, line_items=[LineItem(description="Grinder", quantity=1, unit_price=60000, amount=60000)])
    assert "over_limit" in codes(big)
    assert "foreign_currency" in codes(make(currency="USD"))
    assert "low_confidence" in codes(make(confidence=0.5))


def test_bank_change_is_flagged():
    assert "bank_details_changed" in codes(make(bank_account="UnionBank 1099-8877-6655"))


def test_soft_issues():
    found = validate(make(counterparty="Bukid Kape Co", due_date=""), Policy(), SUPPLIERS, Store(":memory:"), "doc")
    assert {(i.code, i.severity) for i in found} == {("unknown_supplier", "soft"), ("missing_due_date", "soft")}


def test_duplicate_invoice():
    store = Store(":memory:")
    store.upsert("first", counterparty="Bukid Coffee Beans Trading", document_number="INV-1", status="done",
                 filename="a.txt")
    assert "duplicate" in codes(make(counterparty="Bukid Coffee"), store)


def test_other_documents_skip_checks():
    assert codes(make(doc_type="other", total=None, counterparty="")) == set()
