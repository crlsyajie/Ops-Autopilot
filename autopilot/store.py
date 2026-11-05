"""SQLite store: one row per document plus an event trace for every step.

The trace is what makes the system auditable: for any document you can see
what was extracted, which rules fired, what the agent did and who approved."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT, source TEXT, archived_path TEXT,
    received_at TEXT, updated_at TEXT,
    status TEXT,              -- processing | done | needs_review | ignored | rejected | failed | dead
    attempts INTEGER DEFAULT 0,
    doc_type TEXT, counterparty TEXT, document_number TEXT, total REAL, currency TEXT,
    extraction TEXT, issues TEXT, resolution TEXT,
    route TEXT, reason TEXT,
    cost_usd REAL DEFAULT 0, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    error TEXT, preview TEXT, reviewed_by TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT, ts TEXT, step TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_doc ON events(doc_id);
"""

JSON_COLUMNS = ("extraction", "issues", "resolution")
FINAL_STATUSES = ("done", "needs_review", "ignored", "rejected", "processing")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    # -------------------------------------------------------------- documents

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for col in JSON_COLUMNS:
            if out.get(col):
                out[col] = json.loads(out[col])
        return out

    def get(self, doc_id: str) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())

    def upsert(self, doc_id: str, **fields: Any) -> None:
        for col in JSON_COLUMNS:
            if col in fields and fields[col] is not None and not isinstance(fields[col], str):
                fields[col] = json.dumps(fields[col])
        fields["updated_at"] = now()
        with self.lock:
            exists = self.conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*fields.values(), doc_id))
            else:
                fields.setdefault("received_at", now())
                cols = ", ".join(["id", *fields])
                marks = ", ".join("?" for _ in range(len(fields) + 1))
                self.conn.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", (doc_id, *fields.values()))
            self.conn.commit()

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM documents", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY received_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def find_duplicates(self, counterparty: str, document_number: str, exclude_id: str) -> list[dict]:
        if not counterparty or not document_number:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, filename, status, total, received_at FROM documents "
                "WHERE lower(counterparty) = lower(?) AND document_number = ? AND id != ? "
                "AND status IN ('done', 'needs_review')",
                (counterparty, document_number, exclude_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def similar(self, counterparty: str, document_number: str, total: float | None, exclude_id: str) -> list[dict]:
        """Looser search for the agent: same number, or same supplier word and same total."""
        first_word = (counterparty.split() or [""])[0]
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, filename, status, counterparty, document_number, total, received_at FROM documents "
                "WHERE id != ? AND status IN ('done', 'needs_review') AND ("
                "  (document_number != '' AND document_number = ?) OR "
                "  (? != '' AND lower(counterparty) LIKE lower(?) AND total = ?))",
                (exclude_id, document_number, first_word, f"%{first_word}%", total),
            ).fetchall()
        return [dict(r) for r in rows][:10]

    # ----------------------------------------------------------------- events

    def event(self, doc_id: str, step: str, detail: Any = None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (doc_id, ts, step, detail) VALUES (?, ?, ?, ?)",
                (doc_id, now(), step, json.dumps(detail, default=str)),
            )
            self.conn.commit()

    def events(self, doc_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM events WHERE doc_id = ? ORDER BY id", (doc_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    # ---------------------------------------------------------------- metrics

    def metrics(self, manual_minutes_per_document: float) -> dict:
        with self.lock:
            counts = {r["status"]: r["n"] for r in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status")}
            agg = self.conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(AVG(latency_ms), 0) AS lat, "
                "SUM(CASE WHEN route = 'auto' THEN 1 ELSE 0 END) AS auto_n, "
                "SUM(CASE WHEN status IN ('done', 'ignored') THEN 1 ELSE 0 END) AS handled "
                "FROM documents").fetchone()
        n = agg["n"] or 0
        decided = sum(counts.get(s, 0) for s in ("done", "needs_review", "ignored", "rejected"))
        auto_n = agg["auto_n"] or 0
        return {
            "documents": n,
            "by_status": counts,
            "auto_rate": round(auto_n / decided, 3) if decided else 0.0,
            "review_queue": counts.get("needs_review", 0),
            "failed": counts.get("failed", 0) + counts.get("dead", 0),
            "cost_usd": round(agg["cost"], 4),
            "avg_cost_usd": round(agg["cost"] / n, 4) if n else 0.0,
            "avg_latency_s": round((agg["lat"] or 0) / 1000, 1),
            # Only documents finished without a person count as time saved.
            "hours_saved": round(auto_n * manual_minutes_per_document / 60, 1),
        }
