"""Shared fixtures. Tests run against the demo warehouse; it is generated once if missing."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="session")
def db_path():
    from bi_copilot.config import settings
    if not settings.db_path.exists():
        from bi_copilot.data.generator import generate
        generate(customers=12_000)
    return settings.db_path


@pytest.fixture(scope="session")
def db(db_path):
    from bi_copilot.db import get_db
    return get_db(str(db_path))


@pytest.fixture(scope="session")
def copilot(db):
    from bi_copilot.copilot import Copilot
    return Copilot(db=db, llm=None)


@pytest.fixture(scope="session")
def tr():
    from bi_copilot.semantic.timeparse import month_range, quarter_range, year_range
    return dict(q2_25=quarter_range(2025, 2), q2_24=quarter_range(2024, 2), y25=year_range(2025), y24=year_range(2024),
                oct24=month_range(2024, 10), oct23=month_range(2023, 10))
