"""Central settings. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else default


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: _env_path("BI_COPILOT_DB", REPO_ROOT / "data" / "halden.duckdb"))
    schema_sql: Path = REPO_ROOT / "warehouse" / "schema.sql"
    views_sql: Path = REPO_ROOT / "warehouse" / "views.sql"
    metrics_yaml: Path = REPO_ROOT / "config" / "metrics.yaml"
    events_yaml: Path = REPO_ROOT / "eval" / "ground_truth" / "planted_events.yaml"
    knowledge_dir: Path = REPO_ROOT / "knowledge_base"
    benchmark_yaml: Path = REPO_ROOT / "eval" / "benchmark" / "questions.yaml"
    results_dir: Path = REPO_ROOT / "eval" / "results"

    # Guardrails
    query_timeout_s: float = float(os.getenv("BI_COPILOT_QUERY_TIMEOUT", "15"))
    max_rows: int = int(os.getenv("BI_COPILOT_MAX_ROWS", "5000"))

    # LLM: "offline" (default, no key needed), "anthropic" or "openai"
    llm_provider: str = os.getenv("BI_COPILOT_LLM_PROVIDER", "offline").lower()
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # Agent
    agent_max_steps: int = int(os.getenv("BI_COPILOT_AGENT_MAX_STEPS", "8"))


# The synthetic history ends here; "today" for relative dates ("last month") is this day.
DATA_START = date(2022, 1, 1)
DATA_END = date(2025, 12, 31)
AS_OF_DATE = DATA_END

settings = Settings()
