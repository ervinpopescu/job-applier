from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure virtual environment packages and src/ are in sys.path even when pytest is invoked globally
os.environ["CF_ACCESS_ENABLED"] = "false"
root = Path(__file__).resolve().parents[1]
venv_site_packages = list((root / ".venv" / "lib").glob("python*/site-packages"))
for sp in venv_site_packages:
    if str(sp) not in sys.path:
        sys.path.insert(0, str(sp))

src_dir = root / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Guarantees that test runs never write mock data into the production database or CSV tracker."""
    test_db = tmp_path / "test_job_applier.db"
    test_csv = tmp_path / "test_tracker.csv"
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(test_db))
    monkeypatch.setenv("JOB_APPLIER_TRACKER_PATH", str(test_csv))
    monkeypatch.setenv("CF_ACCESS_ENABLED", "false")
