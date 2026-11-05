"""Eval runner: process every case in evals/cases with a fresh database, then
score the result against the hand-labelled answer in evals/expected.

    python evals/run_evals.py                 # live model if a key is set, else replay
    python evals/run_evals.py --mode live     # require the real model
    python evals/run_evals.py --mode replay   # plumbing only (answers come from the labels)

Scores
- field accuracy: did extraction read each field right (before any agent fix)
- route accuracy: did the document end up auto / review / ignore as it should
- issue recall:   were the expected problems flagged
- cost and time per document

Exit code 1 when route accuracy falls below --min-route-accuracy, so CI can block a bad prompt change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopilot.config import Settings  # noqa: E402
from autopilot.pipeline import Autopilot  # noqa: E402
from autopilot.validate import find_supplier  # noqa: E402

DEFAULT_FIELDS = ["doc_type", "counterparty", "document_number", "document_date", "due_date", "currency", "total",
                  "line_items"]


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def field_ok(name: str, got, want, suppliers: list[dict]) -> bool:
    if name == "line_items":
        return len(got or []) == len(want or [])
    if name in ("total", "subtotal", "tax"):
        if got is None or want is None:
            return got is None and want is None
        return abs(float(got) - float(want)) <= 0.01
    if name == "counterparty":
        a, b = find_supplier(got or "", suppliers), find_supplier(want or "", suppliers)
        if a and b:
            return a["name"] == b["name"]
    return norm(got) == norm(want)


def run(mode: str, min_route_accuracy: float) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(data_dir=Path(tmp), llm_mode=mode)
        pilot = Autopilot(settings)
        if mode == "live" and not pilot.live:
            print("No Anthropic credentials found. Set ANTHROPIC_API_KEY or use --mode replay.")
            return 2
        suppliers = settings.suppliers()
        rows = []
        for case in sorted((ROOT / "evals" / "cases").iterdir()):
            exp = json.loads((ROOT / "evals" / "expected" / f"{case.stem}.json").read_text())
            row = pilot.process(case, source="eval")
            extracted = next((e["detail"] for e in pilot.store.events(row["id"]) if e["step"] == "extracted"), {}) or {}
            want = exp["extraction"]
            fields = exp.get("check_fields") or DEFAULT_FIELDS + (
                ["counterparty_email"] if want["doc_type"] == "customer_order" else [])
            field_results = {f: field_ok(f, extracted.get(f), want.get(f), suppliers) for f in fields}
            want_route = exp.get("replay_route", exp["expected_route"]) if not pilot.live else exp["expected_route"]
            got_issues = {i["code"] for i in (row.get("issues") or [])}
            rows.append({
                "case": case.name,
                "route": row.get("route"), "expected_route": want_route,
                "route_ok": row.get("route") == want_route,
                "fields": field_results,
                "missed_fields": [f for f, ok in field_results.items() if not ok],
                "expected_issues": exp["expected_issues"], "issues": sorted(got_issues),
                "issues_found": [i for i in exp["expected_issues"] if i in got_issues],
                "agent": (row.get("resolution") or {}).get("action"),
                "cost_usd": row.get("cost_usd") or 0.0, "latency_s": (row.get("latency_ms") or 0) / 1000,
                "error": row.get("error"),
            })

    n = len(rows)
    field_checks = [ok for r in rows for ok in r["fields"].values()]
    expected_issue_count = sum(len(r["expected_issues"]) for r in rows)
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "live" if pilot.live else "replay",
        "model": settings.model if pilot.live else "replay",
        "cases": n,
        "route_accuracy": round(sum(r["route_ok"] for r in rows) / n, 3),
        "field_accuracy": round(sum(field_checks) / len(field_checks), 3),
        "issue_recall": round(sum(len(r["issues_found"]) for r in rows) / expected_issue_count, 3)
        if expected_issue_count else 1.0,
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "avg_cost_usd": round(sum(r["cost_usd"] for r in rows) / n, 4),
        "avg_latency_s": round(sum(r["latency_s"] for r in rows) / n, 2),
        "errors": sum(1 for r in rows if r["error"]),
    }

    out = ROOT / "evals" / "results"
    out.mkdir(exist_ok=True)
    (out / f"latest_{summary['mode']}.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))
    lines = [
        f"# Eval results ({summary['mode']}, {summary['model']})", "",
        f"Run {summary['run_at']} on {n} cases.", "",
        "| Metric | Value |", "|---|---|",
        f"| Route accuracy | {summary['route_accuracy']:.0%} |",
        f"| Field accuracy | {summary['field_accuracy']:.0%} |",
        f"| Issue recall | {summary['issue_recall']:.0%} |",
        f"| Cost per document | ${summary['avg_cost_usd']:.4f} |",
        f"| Time per document | {summary['avg_latency_s']:.1f}s |",
        f"| Errors | {summary['errors']} |", "",
        "| Case | Route (expected) | Missed fields | Issues flagged | Agent |", "|---|---|---|---|---|",
    ]
    for r in rows:
        mark = "ok" if r["route_ok"] else "**WRONG**"
        lines.append(f"| {r['case']} | {r['route']} ({r['expected_route']}) {mark} | {', '.join(r['missed_fields']) or '-'} "
                     f"| {', '.join(r['issues']) or '-'} | {r['agent'] or '-'} |")
    if summary["mode"] == "replay":
        lines += ["", "_Replay mode answers from the labels, so field accuracy is 100% by design. "
                      "It checks the rules, routing and plumbing, not the model._"]
    (out / f"latest_{summary['mode']}.md").write_text("\n".join(lines) + "\n")

    print("\n".join(lines))
    return 0 if summary["route_accuracy"] >= min_route_accuracy else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["auto", "live", "replay"], default="auto")
    p.add_argument("--min-route-accuracy", type=float, default=0.9)
    a = p.parse_args()
    sys.exit(run(a.mode, a.min_route_accuracy))
