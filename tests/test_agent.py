from types import SimpleNamespace

from conftest import CASES, ScriptedClient, message, tool_use

from autopilot.pipeline import Autopilot

GOOD_RESOLUTION = {
    "action": "auto_approve", "canonical_supplier": "Manila Paper Supply Co.", "due_date": "2026-11-24",
    "reason": "Same supplier as 'Manila Paper Supply Co.' (same bank account); 30-day terms from 2026-10-25.",
    "confidence": 0.93,
}


def test_agent_resolves_soft_issues_and_code_rechecks(settings):
    client = ScriptedClient([
        message(tool_use("lookup_supplier", {"name": "Manila Paper Supply Company"}, "t1"),
                tool_use("find_similar_documents",
                         {"counterparty": "Manila Paper Supply Company", "document_number": "MPS-7781", "total": 5700},
                         "t2")),
        message(tool_use("submit_resolution", GOOD_RESOLUTION, "t3")),
    ])
    pilot = Autopilot(settings, client=client, live=False)
    row = pilot.process(CASES / "04_paper_invoice_variant.txt")

    assert row["route"] == "auto" and row["status"] == "done"
    assert row["counterparty"] == "Manila Paper Supply Co."
    assert row["extraction"]["due_date"] == "2026-11-24"

    # Both tool results went back in ONE user message, matched to their tool_use ids.
    second_request = client.agent_requests[1]["messages"]
    results = second_request[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "Manila Paper Supply Co." in results[0]["content"]

    steps = [e["step"] for e in pilot.store.events(row["id"])]
    assert steps.count("agent_tool") == 2 and "resolution_applied" in steps and "revalidated" in steps


def test_agent_cannot_invent_a_supplier(settings):
    bad = {**GOOD_RESOLUTION, "canonical_supplier": "Some Supplier That Is Not On File"}
    client = ScriptedClient([message(tool_use("submit_resolution", bad, "t1"))])
    pilot = Autopilot(settings, client=client, live=False)
    row = pilot.process(CASES / "04_paper_invoice_variant.txt")
    # The due date was verifiable and got applied; the supplier was not, so a person decides.
    assert row["route"] == "review"
    assert {i["code"] for i in row["issues"]} == {"unknown_supplier"}


def test_low_confidence_recommendation_is_ignored(settings):
    unsure = {**GOOD_RESOLUTION, "confidence": 0.4}
    client = ScriptedClient([message(tool_use("submit_resolution", unsure, "t1"))])
    row = Autopilot(settings, client=client, live=False).process(CASES / "04_paper_invoice_variant.txt")
    assert row["route"] == "review"


def test_invalid_submission_gets_an_error_and_a_retry(settings):
    client = ScriptedClient([
        message(tool_use("submit_resolution", {"action": "pay_now"}, "t1")),
        message(tool_use("submit_resolution", GOOD_RESOLUTION, "t2")),
    ])
    row = Autopilot(settings, client=client, live=False).process(CASES / "04_paper_invoice_variant.txt")
    first_result = client.agent_requests[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] is True
    assert row["route"] == "auto"


def test_step_limit_falls_back_to_a_person(settings):
    settings.agent_max_steps = 3
    looping = [message(tool_use("get_policy", {}, f"t{i}")) for i in range(3)]
    row = Autopilot(settings, client=ScriptedClient(looping), live=False).process(
        CASES / "04_paper_invoice_variant.txt")
    assert row["route"] == "review"
    assert "step limit" in row["resolution"]["reason"]


def test_refusal_falls_back_to_a_person(settings):
    refused = SimpleNamespace(content=[], stop_reason="refusal", usage=None, model="claude-opus-5-5")
    row = Autopilot(settings, client=ScriptedClient([refused]), live=False).process(
        CASES / "04_paper_invoice_variant.txt")
    assert row["route"] == "review" and row["resolution"]["action"] == "needs_human"


def test_agent_is_not_called_when_a_blocking_issue_exists(settings):
    client = ScriptedClient([])  # any agent call would fail: no scripted turns
    row = Autopilot(settings, client=client, live=False).process(CASES / "09_usd_parts_invoice.txt")
    assert row["route"] == "review" and client.agent_requests == []


def test_cost_is_tracked(settings):
    client = ScriptedClient([message(tool_use("submit_resolution", GOOD_RESOLUTION, "t1"))])
    row = Autopilot(settings, client=client, live=False).process(CASES / "04_paper_invoice_variant.txt")
    # One scripted agent call: 1,000 input and 200 output tokens at Opus 5.5 prices ($4 / $20 per million).
    assert abs(row["cost_usd"] - (1000 * 4 + 200 * 20) / 1_000_000) < 1e-9
