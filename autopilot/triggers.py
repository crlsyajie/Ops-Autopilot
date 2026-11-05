"""Triggers: how documents get into the inbox folder.

Everything lands in var/inbox/ first and the pipeline only ever reads from
there. That keeps each trigger tiny and lets you add new ones (a WhatsApp bot,
a Drive folder, an n8n flow that drops files) without touching the pipeline.
"""

from __future__ import annotations

import email
import imaplib
import os
import time
from email import policy
from pathlib import Path

from .pipeline import Autopilot


def watch_folder(pilot: Autopilot, interval: float = 5.0, once: bool = False) -> None:
    """Poll the inbox folder and process new files, then retry failures."""
    print(f"Watching {pilot.settings.inbox_dir} every {interval:g}s (Ctrl+C to stop)")
    while True:
        for row in pilot.run_inbox():
            print(f"  {row['filename']}: {row['status']} ({row.get('reason') or row.get('error') or ''})")
        pilot.retry_failed()
        if once:
            return
        time.sleep(interval)


def fetch_imap(inbox_dir: Path, mark_seen: bool = True) -> list[Path]:
    """Save unread emails (as .eml) from an IMAP mailbox into the inbox folder.

    Works with Gmail using an app password:
      IMAP_HOST=imap.gmail.com IMAP_USER=you@gmail.com IMAP_PASSWORD=<app password>
    Optional IMAP_FOLDER (default INBOX) and IMAP_SEARCH (default UNSEEN).
    """
    host, user, password = os.getenv("IMAP_HOST"), os.getenv("IMAP_USER"), os.getenv("IMAP_PASSWORD")
    if not (host and user and password):
        raise RuntimeError("Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD to read email.")
    saved: list[Path] = []
    with imaplib.IMAP4_SSL(host) as box:
        box.login(user, password)
        box.select(os.getenv("IMAP_FOLDER", "INBOX"))
        _, data = box.search(None, os.getenv("IMAP_SEARCH", "UNSEEN"))
        for num in data[0].split():
            _, parts = box.fetch(num, "(BODY.PEEK[])")
            raw = parts[0][1]
            msg = email.message_from_bytes(raw, policy=policy.default)
            uid = (msg.get("Message-ID") or num.decode()).strip("<>").replace("/", "_")[:80]
            path = inbox_dir / f"email_{uid}.eml"
            path.write_bytes(raw)
            saved.append(path)
            if mark_seen:
                box.store(num, "+FLAGS", "\\Seen")
    return saved
