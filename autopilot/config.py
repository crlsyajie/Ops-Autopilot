"""Settings, read once from environment variables and config/policy.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from .env without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

# Per-million-token prices in USD (Claude API first-party rates).
# input, output, cache read. Cache writes cost 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


@dataclass
class Policy:
    """Business rules. Edit config/policy.json, not this class."""

    home_currency: str = "PHP"
    auto_approve_limit: float = 50_000.0
    min_confidence: float = 0.85
    total_tolerance: float = 1.0
    require_known_supplier: bool = True
    manual_minutes_per_document: float = 6.0

    @classmethod
    def load(cls, path: Path) -> "Policy":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AUTOPILOT_DATA_DIR", ROOT / "var")))
    llm_mode: str = field(default_factory=lambda: os.getenv("AUTOPILOT_LLM", "auto"))  # auto | live | replay
    model: str = field(default_factory=lambda: os.getenv("AUTOPILOT_MODEL", "claude-opus-5-5"))
    extract_effort: str = field(default_factory=lambda: os.getenv("AUTOPILOT_EXTRACT_EFFORT", "medium"))
    agent_effort: str = field(default_factory=lambda: os.getenv("AUTOPILOT_AGENT_EFFORT", "high"))
    agent_max_steps: int = field(default_factory=lambda: int(os.getenv("AUTOPILOT_AGENT_MAX_STEPS", "6")))
    max_attempts: int = 3
    slack_webhook_url: str = field(default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", ""))
    sheets_webhook_url: str = field(default_factory=lambda: os.getenv("SHEETS_WEBHOOK_URL", ""))
    public_base_url: str = field(default_factory=lambda: os.getenv("AUTOPILOT_PUBLIC_URL", "http://127.0.0.1:8000"))
    policy: Policy = field(default_factory=lambda: Policy.load(ROOT / "config" / "policy.json"))
    suppliers_path: Path = field(default_factory=lambda: ROOT / "config" / "suppliers.json")
    replay_dir: Path = field(default_factory=lambda: ROOT / "evals" / "expected")

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autopilot.db"

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.archive_dir, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)

    def suppliers(self) -> list[dict]:
        if not self.suppliers_path.exists():
            return []
        return json.loads(self.suppliers_path.read_text())
