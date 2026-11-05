"""Step 3, Triage agent: runs only when validation leaves "soft" issues.

This is the one real agent in the system: Claude decides which tools to call
and in what order. Everything around it is a fixed workflow. The loop is
written by hand (no framework) so each part of an agent is visible:

    goal + context  ->  model picks a tool  ->  code runs it  ->  result goes back
                    ->  repeat until submit_resolution, a refusal, or the step limit

Guardrails:
- The agent only proposes. Its answer is applied, then validate.py runs again.
- A hard step limit, and any failure falls back to "needs_human".
- Tools are read-only. Nothing the agent calls can pay, write or send.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import asdict
from typing import Any, Callable

from pydantic import ValidationError

from .config import Settings
from .llm import LIVE_EXTRAS, CostMeter
from .schemas import Extraction, Issue, Resolution
from .store import Store
from .validate import normalize


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "lookup_supplier",
        "description": "Search the café's supplier list by name. Returns the closest matches with their official "
                       "name, aliases, payment terms in days and the last 4 digits of their bank account on file.",
        "strict": True,
        "input_schema": _schema({"name": {"type": "string", "description": "Supplier name as written on the document."}}),
    },
    {
        "name": "find_similar_documents",
        "description": "Search documents already processed for possible duplicates: the same document number, or the "
                       "same supplier with the same total.",
        "strict": True,
        "input_schema": _schema({
            "counterparty": {"type": "string"},
            "document_number": {"type": "string"},
            "total": {"type": "number"},
        }),
    },
    {
        "name": "get_policy",
        "description": "Return the café's approval rules (limits, confidence threshold, home currency).",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "submit_resolution",
        "description": "Finish triage. Call exactly once. action is auto_approve only when every open issue is "
                       "resolved by facts you looked up; otherwise needs_human.",
        "strict": True,
        "input_schema": _schema({
            "action": {"type": "string", "enum": ["auto_approve", "needs_human"]},
            "canonical_supplier": {"type": "string",
                                   "description": "The supplier's official name from lookup_supplier, or empty."},
            "due_date": {"type": "string",
                         "description": "YYYY-MM-DD computed from the supplier's payment terms, or empty."},
            "reason": {"type": "string", "description": "One or two sentences a bookkeeper can check."},
            "confidence": {"type": "number"},
        }),
    },
]

SYSTEM = """You are the exception-triage agent for Kapehan sa Kanto's bookkeeping autopilot.
A document was extracted and checked by rules. Some open issues are "soft": they can be cleared with facts from the
café's own records. Your job is to look those facts up with the tools and recommend what happens next.

How to work:
- Unknown supplier: use lookup_supplier. Treat it as the same supplier only if the match is clearly the same business
  (a short form, a missing "Inc."/"Trading", a typo). Different businesses with similar words are not a match.
- Missing due date: get the supplier's payment terms with lookup_supplier and add them to the document date.
- Always run find_similar_documents once to rule out a duplicate under a different name.
- If anything looks off (a possible duplicate, a mismatch you cannot explain), choose needs_human.

You only recommend. Code re-checks your answer and a person reviews anything you are unsure about.
Finish by calling submit_resolution exactly once."""


def _tool_lookup_supplier(args: dict, suppliers: list[dict], **_: Any) -> dict:
    query = normalize(args.get("name", ""))
    index: dict[str, dict] = {}
    for s in suppliers:
        for n in [s["name"], *s.get("aliases", [])]:
            index[normalize(n)] = s
    scored = []
    for key, s in index.items():
        score = difflib.SequenceMatcher(None, query, key).ratio()
        if query and (query in key or key in query):
            score = max(score, 0.9)
        scored.append((score, s))
    seen, matches = set(), []
    for score, s in sorted(scored, key=lambda x: -x[0]):
        if s["name"] in seen or score < 0.5:
            continue
        seen.add(s["name"])
        matches.append({
            "name": s["name"], "aliases": s.get("aliases", []), "score": round(score, 2),
            "payment_terms_days": s.get("payment_terms_days"),
            "bank_account_last4": (s.get("bank_account") or "")[-4:],
        })
    return {"matches": matches[:3]}


def _tool_find_similar(args: dict, store: Store, doc_id: str, **_: Any) -> dict:
    return {"documents": store.similar(args.get("counterparty", ""), args.get("document_number", ""),
                                       args.get("total"), doc_id)}


def _tool_get_policy(args: dict, settings: Settings, **_: Any) -> dict:
    return asdict(settings.policy)


HANDLERS: dict[str, Callable[..., dict]] = {
    "lookup_supplier": _tool_lookup_supplier,
    "find_similar_documents": _tool_find_similar,
    "get_policy": _tool_get_policy,
}


def _fallback(reason: str) -> Resolution:
    return Resolution(action="needs_human", canonical_supplier="", due_date="", reason=reason, confidence=0.0)


def triage(
    client: Any, settings: Settings, ext: Extraction, issues: list[Issue], store: Store,
    doc_id: str, meter: CostMeter, live: bool, log: Callable[[str, Any], None],
) -> Resolution:
    suppliers = settings.suppliers()
    context = {"extraction": ext.model_dump(), "open_issues": [i.model_dump() for i in issues]}
    messages: list[dict] = [{
        "role": "user",
        "content": "Triage this document.\n\n" + json.dumps(context, indent=2),
    }]
    extras = LIVE_EXTRAS if live else {}
    nudged = False

    for step in range(1, settings.agent_max_steps + 1):
        response = client.beta.messages.create(
            model=settings.model,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": settings.agent_effort},
            **extras,
        )
        meter.add(response, settings.model)

        if response.stop_reason == "refusal":
            return _fallback("The model declined to triage this document.")
        # Keep the full assistant turn (including thinking blocks): the history stays append-only.
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue

        calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not calls:
            if nudged:
                return _fallback("The agent stopped without submitting a resolution.")
            nudged = True
            messages.append({"role": "user", "content": "Call submit_resolution to finish."})
            continue

        results = []
        for call in calls:
            if call.name == "submit_resolution":
                try:
                    resolution = Resolution(**call.input)
                except ValidationError as e:
                    results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                    "content": f"Invalid resolution: {e.errors()[0]['msg']}. Try again."})
                    continue
                resolution.confidence = max(0.0, min(1.0, resolution.confidence))
                log("agent_submit", {"step": step, **resolution.model_dump()})
                return resolution

            handler = HANDLERS.get(call.name)
            if handler is None:
                output, is_error = {"error": f"Unknown tool {call.name}"}, True
            else:
                try:
                    output, is_error = handler(call.input, suppliers=suppliers, store=store,
                                               doc_id=doc_id, settings=settings), False
                except Exception as e:  # a broken tool must not crash the run
                    output, is_error = {"error": str(e)}, True
            log("agent_tool", {"step": step, "tool": call.name, "input": call.input, "output": output})
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(output, default=str), "is_error": is_error})

        # All results from one turn go back together in a single user message.
        messages.append({"role": "user", "content": results})

    return _fallback(f"The agent hit the {settings.agent_max_steps}-step limit.")
