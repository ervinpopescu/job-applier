from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent


def _read_json(filename: str) -> dict[str, Any]:
    file_path = CONFIG_DIR / filename
    if not file_path.exists():
        return {}
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Notice: Failed loading config {filename}: {e}")
        return {}


@lru_cache(maxsize=1)
def get_regions_config() -> dict[str, Any]:
    """Returns parsed regions.json data."""
    return _read_json("regions.json")


@lru_cache(maxsize=1)
def get_job_filters_config() -> dict[str, Any]:
    """Returns parsed job_filters.json data."""
    return _read_json("job_filters.json")


@lru_cache(maxsize=1)
def get_ats_companies_config() -> dict[str, Any]:
    """Returns parsed ats_companies.json data."""
    return _read_json("ats_companies.json")


@lru_cache(maxsize=1)
def get_platforms_config() -> dict[str, Any]:
    """Returns parsed platforms.json data."""
    return _read_json("platforms.json")
