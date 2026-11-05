"""The model layer: one place that knows how to reach Claude, what it cost,
and how to run without a key (replay mode).

Live mode uses the official Anthropic SDK. Replay mode answers from the
hand-labelled files in evals/expected/, so the whole pipeline, the tests and
the dashboard demo run offline. Replay tests the plumbing, never the model.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import PRICES, Settings
from .schemas import Extraction

# Opus 5.5 can decline a request (stop_reason "refusal"). With server-side
# fallbacks the API retries on a fallback model inside the same call.
LIVE_EXTRAS: dict[str, Any] = {
    "betas": ["server-side-fallback-2026-07-01"],
    "fallbacks": "default",
}


@dataclass
class CostMeter:
    """Adds up tokens and dollars across every model call for one document."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    calls: int = 0
    models: set[str] = field(default_factory=set)

    def add(self, response: Any, model: str) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        i = getattr(usage, "input_tokens", 0) or 0
        o = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        p_in, p_out, p_cache = PRICES.get(model, (0.0, 0.0, 0.0))
        self.input_tokens += i
        self.output_tokens += o
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        self.usd += (i * p_in + o * p_out + cr * p_cache + cw * p_in * 1.25) / 1_000_000
        self.calls += 1
        self.models.add(getattr(response, "model", None) or model)


def has_live_credentials() -> bool:
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def make_client(settings: Settings) -> tuple[Any, bool]:
    """Returns (client, is_live)."""
    mode = settings.llm_mode
    if mode == "live" or (mode == "auto" and has_live_credentials()):
        import anthropic

        return anthropic.Anthropic(max_retries=4, timeout=180.0), True
    return ReplayClient(settings.replay_dir), False


# ---------------------------------------------------------------- replay mode

_ZERO_USAGE = SimpleNamespace(
    input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
)
_SOURCE_RE = re.compile(r"Source file: (\S+)")
_PREFIX_RE = re.compile(r"^([0-9a-f]{16}_|\d{10,}_)")


def _find_source(messages: list[dict]) -> str | None:
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                m = _SOURCE_RE.search(block.get("text") or "")
                if m:
                    return m.group(1)
    return None


class _ReplayMessages:
    def __init__(self, replay_dir: Path):
        self.replay_dir = replay_dir

    def parse(self, *, messages: list[dict], **_: Any) -> Any:
        source = _find_source(messages)
        # Uploads get a timestamp prefix and archived files a doc-id prefix; strip either.
        stem = _PREFIX_RE.sub("", Path(source).stem) if source else ""
        path = self.replay_dir / f"{stem}.json" if stem else None
        if path and path.exists():
            extraction = Extraction(**json.loads(path.read_text())["extraction"])
        else:
            extraction = Extraction(
                doc_type="other", counterparty="", counterparty_email="", document_number="",
                document_date="", due_date="", currency="", line_items=[], subtotal=None,
                tax=None, total=None, bank_account="", confidence=0.0,
                notes="Replay mode: no recorded answer for this file.",
            )
        return SimpleNamespace(
            parsed_output=extraction, stop_reason="end_turn", usage=_ZERO_USAGE,
            model="replay", content=[],
        )

    def create(self, **_: Any) -> Any:
        # The triage agent in replay mode always hands the document to a person.
        call = SimpleNamespace(
            type="tool_use", id="toolu_replay", name="submit_resolution",
            input={
                "action": "needs_human", "canonical_supplier": "", "due_date": "",
                "reason": "Replay mode has no model, so a person must check this.",
                "confidence": 0.0,
            },
        )
        return SimpleNamespace(content=[call], stop_reason="tool_use", usage=_ZERO_USAGE, model="replay")


class ReplayClient:
    def __init__(self, replay_dir: Path):
        messages = _ReplayMessages(replay_dir)
        self.messages = messages
        self.beta = SimpleNamespace(messages=messages)
