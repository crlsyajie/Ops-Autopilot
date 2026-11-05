"""The orchestrator. One document goes through fixed steps:

    load -> extract (Claude) -> validate (rules) -> triage (agent, only if needed)
         -> decide (policy) -> act or queue for review -> log

Every step writes an event, every document has an attempt counter, and a
failure on one document never stops the others.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from . import actions
from .agent import triage
from .config import Settings
from .decide import apply_resolution, decide
from .extract import extract
from .llm import CostMeter, make_client
from .loaders import SUPPORTED, load
from .schemas import Extraction
from .store import FINAL_STATUSES, Store
from .validate import find_supplier, validate


class Autopilot:
    def __init__(self, settings: Settings | None = None, client: Any = None, live: bool | None = None,
                 store: Store | None = None):
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        if client is None:
            client, live = make_client(self.settings)
        self.client = client
        self.live = bool(live)
        self.store = store or Store(self.settings.db_path)

    # ------------------------------------------------------------------ intake

    def run_inbox(self) -> list[dict]:
        files = sorted(p for p in self.settings.inbox_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in SUPPORTED)
        return [self.process(p, source="inbox") for p in files]

    def retry_failed(self) -> list[dict]:
        out = []
        for row in self.store.list(status="failed", limit=500):
            if row.get("archived_path") and Path(row["archived_path"]).exists():
                out.append(self.process(Path(row["archived_path"]), source=row.get("source") or "retry", retry=True))
        return out

    def _archive(self, path: Path, doc_id: str) -> Path:
        target = self.settings.archive_dir / f"{doc_id}_{path.name}"
        if path.resolve() != target.resolve():
            if not target.exists():
                shutil.copy2(path, target)
            if path.parent.resolve() == self.settings.inbox_dir.resolve():
                path.unlink(missing_ok=True)
        return target

    # ---------------------------------------------------------------- pipeline

    def process(self, path: Path, source: str = "manual", retry: bool = False) -> dict:
        doc = load(path)
        existing = self.store.get(doc.doc_id)
        if existing and existing["status"] in FINAL_STATUSES and not retry:
            self.store.event(doc.doc_id, "duplicate_file_skipped", {"filename": path.name})
            self._archive(path, doc.doc_id)
            return existing

        attempts = (existing["attempts"] if existing else 0) + 1
        archived = self._archive(path, doc.doc_id)
        self.store.upsert(doc.doc_id, filename=doc.filename, source=source, archived_path=str(archived),
                          status="processing", attempts=attempts, preview=doc.preview, error=None)

        def log(step: str, detail: Any = None) -> None:
            self.store.event(doc.doc_id, step, detail)

        log("received", {"filename": doc.filename, "source": source, "attempt": attempts, "live_model": self.live})
        meter = CostMeter()
        started = time.monotonic()
        try:
            ext = extract(self.client, self.settings, doc, meter, self.live)
            log("extracted", ext.model_dump())

            policy, suppliers = self.settings.policy, self.settings.suppliers()
            issues = validate(ext, policy, suppliers, self.store, doc.doc_id)
            log("validated", [i.model_dump() for i in issues])

            resolution = None
            soft_only = issues and all(i.severity == "soft" for i in issues)
            if soft_only:
                resolution = triage(self.client, self.settings, ext, issues, self.store, doc.doc_id,
                                    meter, self.live, log)
                changes = apply_resolution(ext, resolution, suppliers, policy.min_confidence)
                if changes:
                    log("resolution_applied", changes)
                    issues = validate(ext, policy, suppliers, self.store, doc.doc_id)
                    log("revalidated", [i.model_dump() for i in issues])

            # Store the official supplier name so later duplicate checks line up.
            supplier = find_supplier(ext.counterparty, suppliers) if ext.doc_type in ("invoice", "receipt") else None
            if supplier:
                ext.counterparty = supplier["name"]

            decision = decide(ext, issues)
            log("decided", decision.model_dump())
            self.store.upsert(
                doc.doc_id, doc_type=ext.doc_type, counterparty=ext.counterparty,
                document_number=ext.document_number, total=ext.total, currency=ext.currency,
                extraction=ext.model_dump(), issues=[i.model_dump() for i in issues],
                resolution=resolution.model_dump() if resolution else None,
                route=decision.route, reason=decision.reason,
            )

            if decision.route == "auto":
                actions.record(self.settings, doc.doc_id, doc.filename, ext, "autopilot", log)
                status = "done"
            elif decision.route == "review":
                actions.notify_review(self.settings, doc.doc_id, doc.filename, decision.reason, log)
                status = "needs_review"
            else:
                status = "ignored"
            self.store.upsert(doc.doc_id, status=status)
        except Exception as e:
            status = "dead" if attempts >= self.settings.max_attempts else "failed"
            log("error", {"type": type(e).__name__, "message": str(e), "status": status})
            self.store.upsert(doc.doc_id, status=status, error=f"{type(e).__name__}: {e}")
        finally:
            self.store.upsert(
                doc.doc_id, cost_usd=meter.usd, input_tokens=meter.input_tokens,
                output_tokens=meter.output_tokens, latency_ms=int((time.monotonic() - started) * 1000),
            )
            log("cost", {"usd": round(meter.usd, 5), "calls": meter.calls,
                         "input_tokens": meter.input_tokens, "output_tokens": meter.output_tokens})
        return self.store.get(doc.doc_id)

    # ---------------------------------------------------------- human review

    def approve(self, doc_id: str, reviewer: str, edits: dict | None = None) -> dict:
        row = self.store.get(doc_id)
        if not row or row["status"] != "needs_review":
            raise ValueError("Only documents waiting for review can be approved.")
        ext = Extraction(**{**row["extraction"], **(edits or {})})

        def log(step: str, detail: Any = None) -> None:
            self.store.event(doc_id, step, detail)

        log("approved", {"by": reviewer, "edits": edits or {}})
        done = actions.record(self.settings, doc_id, row["filename"], ext, reviewer, log)
        self.store.upsert(doc_id, status="done", reviewed_by=reviewer, extraction=ext.model_dump(),
                          counterparty=ext.counterparty, total=ext.total)
        return {**self.store.get(doc_id), "actions": done}

    def reject(self, doc_id: str, reviewer: str, reason: str = "") -> dict:
        row = self.store.get(doc_id)
        if not row or row["status"] != "needs_review":
            raise ValueError("Only documents waiting for review can be rejected.")
        self.store.event(doc_id, "rejected", {"by": reviewer, "reason": reason})
        self.store.upsert(doc_id, status="rejected", reviewed_by=reviewer)
        return self.store.get(doc_id)

    def metrics(self) -> dict:
        return {**self.store.metrics(self.settings.policy.manual_minutes_per_document), "live_model": self.live,
                "model": self.settings.model if self.live else "replay"}
