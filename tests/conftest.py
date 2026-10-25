"""Test configuration: isolated database and no Groq key, so diagnoses use the deterministic fallback."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp_dir = tempfile.mkdtemp(prefix="looplens-tests-")
os.environ["LOOPLENS_DB_PATH"] = str(Path(_tmp_dir) / "test.db")
os.environ["GROQ_API_KEY"] = ""
os.environ["COST_THRESHOLD_USD"] = "0.05"
os.environ["PRICE_PER_1K_TOKENS_USD"] = "0.002"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402


@pytest.fixture
def client():
    api.database.reset_db()
    api.governance.reset_state()
    return TestClient(api.app)
