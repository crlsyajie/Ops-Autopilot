import csv
import json
import shutil

from conftest import CASES, ROOT

from autopilot.pipeline import Autopilot


def expected(name: str) -> dict:
    return json.loads((ROOT / "evals" / "expected" / f"{name}.json").read_text())


def test_every_case_routes_as_labelled_in_replay(pilot):
    for case in sorted(CASES.iterdir()):
        row = pilot.process(case, source="test")
        exp = expected(case.stem)
        assert row["route"] == exp.get("replay_route", exp["expected_route"]), case.name
        found = {i["code"] for i in row["issues"] or []}
        assert set(exp["expected_issues"]) <= found, case.name


def test_auto_documents_are_recorded_once(pilot):
    case = CASES / "01_bukid_invoice.txt"
    pilot.process(case)
    pilot.process(case)  # same bytes again: skipped, not recorded twice
    with (pilot.settings.out_dir / "payables.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["counterparty"] == "Bukid Coffee Beans Trading"
    assert rows[0]["approved_by"] == "autopilot"


def test_customer_order_gets_reply_draft_not_sent(pilot):
    row = pilot.process(CASES / "03_office_order.eml")
    draft = pilot.settings.out_dir / "reply_drafts" / f"{row['id']}.txt"
    assert row["status"] == "done"
    assert "andrea.santos@example.com" in draft.read_text()


def test_inbox_files_are_archived(pilot):
    shutil.copy2(CASES / "02_dairy_receipt.txt", pilot.settings.inbox_dir / "02_dairy_receipt.txt")
    [row] = pilot.run_inbox()
    assert row["status"] == "done"
    assert list(pilot.settings.inbox_dir.iterdir()) == []
    assert (pilot.settings.archive_dir / f"{row['id']}_02_dairy_receipt.txt").exists()


def test_review_then_approve_records_with_reviewer(pilot):
    row = pilot.process(CASES / "06_bakery_math_error.txt")
    assert row["status"] == "needs_review"
    done = pilot.approve(row["id"], "carlos", {"total": 3180})
    assert done["status"] == "done" and done["reviewed_by"] == "carlos"
    with (pilot.settings.out_dir / "payables.csv").open() as f:
        [ledger] = list(csv.DictReader(f))
    assert float(ledger["total"]) == 3180 and ledger["approved_by"] == "carlos"


def test_reject(pilot):
    row = pilot.process(CASES / "08_dairy_new_bank.eml")
    assert pilot.reject(row["id"], "carlos", "Called supplier: fake email")["status"] == "rejected"
    assert not (pilot.settings.out_dir / "payables.csv").exists()


class BrokenClient:
    class _M:
        def parse(self, **_):
            raise TimeoutError("model timed out")

    def __init__(self):
        self.beta = type("B", (), {"messages": self._M()})()


def test_failures_retry_then_go_dead(settings):
    pilot = Autopilot(settings, client=BrokenClient(), live=False)
    row = pilot.process(CASES / "01_bukid_invoice.txt")
    assert row["status"] == "failed" and "timed out" in row["error"]
    pilot.retry_failed()
    pilot.retry_failed()
    assert pilot.store.get(row["id"])["status"] == "dead"
    assert pilot.store.get(row["id"])["attempts"] == 3


def test_metrics(pilot):
    for case in sorted(CASES.iterdir()):
        pilot.process(case)
    m = pilot.metrics()
    assert m["documents"] == 13
    assert m["review_queue"] == 8
    assert m["by_status"]["ignored"] == 1
    assert m["hours_saved"] == round(4 * 6 / 60, 1)
