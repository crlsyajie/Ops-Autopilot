"""Web app: dashboard, review queue, upload, and a JSON API / webhook that
no-code tools (n8n, Make, Zapier) can call.

Run: python -m autopilot serve   (then open http://127.0.0.1:8000)
No login yet: keep it on localhost or put it behind auth before exposing it.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .loaders import SUPPORTED
from .pipeline import Autopilot

HERE = Path(__file__).parent


def _safe_name(name: str) -> str:
    base = Path(name or "upload.txt").name.replace(" ", "_")
    if Path(base).suffix.lower() not in SUPPORTED:
        raise HTTPException(400, f"Unsupported file type. Use one of: {', '.join(sorted(SUPPORTED))}")
    return f"{int(time.time() * 1000)}_{base}"


class WebhookDocument(BaseModel):
    filename: str
    content_base64: str


def create_app(pilot: Autopilot | None = None) -> FastAPI:
    pilot = pilot or Autopilot()
    app = FastAPI(title="Ops Autopilot")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    app.state.pilot = pilot

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {
            "m": pilot.metrics(),
            "queue": pilot.store.list(status="needs_review", limit=50),
            "recent": pilot.store.list(limit=30),
            "policy": pilot.settings.policy,
        })

    @app.get("/documents/{doc_id}", response_class=HTMLResponse)
    def document(request: Request, doc_id: str):
        row = pilot.store.get(doc_id)
        if not row:
            raise HTTPException(404, "Document not found")
        return templates.TemplateResponse(request, "document.html", {
            "d": row, "events": pilot.store.events(doc_id), "live": pilot.live,
        })

    @app.post("/upload")
    async def upload(file: UploadFile = File(...)):
        path = pilot.settings.inbox_dir / _safe_name(file.filename)
        path.write_bytes(await file.read())
        row = pilot.process(path, source="upload")
        return RedirectResponse(f"/documents/{row['id']}", status_code=303)

    @app.post("/run-inbox")
    def run_inbox():
        pilot.run_inbox()
        return RedirectResponse("/", status_code=303)

    @app.post("/documents/{doc_id}/approve")
    def approve(doc_id: str, reviewer: str = Form("owner"), counterparty: str = Form(""),
                total: str = Form(""), due_date: str = Form("")):
        edits: dict = {}
        if counterparty.strip():
            edits["counterparty"] = counterparty.strip()
        if total.strip():
            try:
                edits["total"] = float(total.replace(",", ""))
            except ValueError:
                raise HTTPException(400, "Total must be a number")
        if due_date.strip():
            edits["due_date"] = due_date.strip()
        try:
            pilot.approve(doc_id, reviewer.strip() or "owner", edits)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    @app.post("/documents/{doc_id}/reject")
    def reject(doc_id: str, reviewer: str = Form("owner"), reason: str = Form("")):
        try:
            pilot.reject(doc_id, reviewer.strip() or "owner", reason)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)

    # -------------------------------------------------------------------- API

    @app.get("/health")
    def health():
        return {"ok": True, "live_model": pilot.live}

    @app.get("/api/metrics")
    def api_metrics():
        return pilot.metrics()

    @app.get("/api/documents")
    def api_documents(status: str | None = None, limit: int = 100):
        return pilot.store.list(status=status, limit=min(limit, 500))

    @app.get("/api/documents/{doc_id}")
    def api_document(doc_id: str):
        row = pilot.store.get(doc_id)
        if not row:
            raise HTTPException(404, "Document not found")
        return {**row, "events": pilot.store.events(doc_id)}

    @app.post("/api/webhook/document")
    def webhook(doc: WebhookDocument):
        """For n8n / Make / Zapier: POST {filename, content_base64}."""
        path = pilot.settings.inbox_dir / _safe_name(doc.filename)
        try:
            path.write_bytes(base64.b64decode(doc.content_base64, validate=True))
        except ValueError:
            raise HTTPException(400, "content_base64 is not valid base64")
        row = pilot.process(path, source="webhook")
        return {"id": row["id"], "status": row["status"], "route": row.get("route"), "reason": row.get("reason")}

    return app
