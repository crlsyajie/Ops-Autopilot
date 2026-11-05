from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopilot.config import Settings  # noqa: E402
from autopilot.llm import ReplayClient  # noqa: E402
from autopilot.pipeline import Autopilot  # noqa: E402

CASES = ROOT / "evals" / "cases"
USAGE = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tool_use(name: str, args: dict, id_: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def message(*blocks, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=USAGE, model="claude-opus-5-5")


class ScriptedClient:
    """Extraction comes from the labelled replay files; the agent's turns are scripted."""

    def __init__(self, agent_turns: list):
        self.replay = ReplayClient(ROOT / "evals" / "expected")
        self.turns = list(agent_turns)
        self.agent_requests: list[dict] = []
        outer = self

        class Messages:
            def parse(self, **kw):
                return outer.replay.messages.parse(**kw)

            def create(self, **kw):
                # Snapshot: the agent keeps appending to the same list after this call.
                outer.agent_requests.append({**kw, "messages": list(kw["messages"])})
                return outer.turns.pop(0)

        self.beta = SimpleNamespace(messages=Messages())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, llm_mode="replay")


@pytest.fixture
def pilot(settings: Settings) -> Autopilot:
    return Autopilot(settings)
