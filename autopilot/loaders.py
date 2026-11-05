"""Turn a file from the inbox into Claude content blocks.

Text and emails become text blocks, PDFs become document blocks and photos
become image blocks, so one extraction call handles every format."""

from __future__ import annotations

import base64
import email
import hashlib
from dataclasses import dataclass
from email import policy
from pathlib import Path

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
TEXT_TYPES = {".txt", ".md", ".csv"}
SUPPORTED = TEXT_TYPES | {".eml", ".pdf"} | set(IMAGE_TYPES)


@dataclass
class LoadedDoc:
    doc_id: str  # sha256 of the bytes: the same file is never processed twice
    filename: str
    blocks: list[dict]
    preview: str


def doc_id_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def _pdf_block(data: bytes) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(data)}}


def _email_blocks(data: bytes) -> tuple[list[dict], str]:
    msg = email.message_from_bytes(data, policy=policy.default)
    header = f"From: {msg.get('From', '')}\nTo: {msg.get('To', '')}\nDate: {msg.get('Date', '')}\nSubject: {msg.get('Subject', '')}"
    body = msg.get_body(preferencelist=("plain", "html"))
    text = header + "\n\n" + (body.get_content() if body else "")
    blocks: list[dict] = []
    for part in msg.iter_attachments():
        name = (part.get_filename() or "").lower()
        payload = part.get_payload(decode=True) or b""
        if name.endswith(".pdf"):
            blocks.append(_pdf_block(payload))
        elif Path(name).suffix in IMAGE_TYPES:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[Path(name).suffix], "data": _b64(payload)}})
    blocks.append({"type": "text", "text": text})
    return blocks, text


def load(path: Path) -> LoadedDoc:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported file type: {suffix}")
    if suffix in TEXT_TYPES:
        text = data.decode("utf-8", errors="replace")
        blocks, preview = [{"type": "text", "text": text}], text
    elif suffix == ".eml":
        blocks, preview = _email_blocks(data)
    elif suffix == ".pdf":
        blocks, preview = [_pdf_block(data)], "(PDF document)"
    else:
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[suffix], "data": _b64(data)}}]
        preview = "(image)"
    return LoadedDoc(doc_id=doc_id_for(data), filename=path.name, blocks=blocks, preview=preview[:4000])
