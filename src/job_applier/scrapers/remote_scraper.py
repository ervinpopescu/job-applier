from __future__ import annotations

import re
from typing import Any

import pandas as pd
import requests

from job_applier.scrapers.region_config import (  # type: ignore[import-not-found]
    RegionScope,
    get_default_region_scope,
)


def scrape_remote_jobs(
    search_term: str = "Cloud",
    count: int = 30,
    region_scope: RegionScope | None = None,
    emea_only: bool = True,
) -> pd.DataFrame:
    """Scrapes remote tech jobs via Jobicy and RemoteOK APIs configured by regional scope."""
    scope = region_scope or (
        get_default_region_scope() if emea_only else RegionScope(region="GLOBAL")
    )
    matched_jobs: list[dict[str, Any]] = []
    term_lower = search_term.lower()
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    # 1. Jobicy API
    try:
        geo_param = scope.get_jobicy_geo_param()
        url = f"https://jobicy.com/api/v2/remote-jobs?count={min(count, 50)}{geo_param}"
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            for j in data.get("jobs", []):
                title = j.get("jobTitle", "")
                title_lower = title.lower()
                desc = j.get("jobDescription", "")
                clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()
                job_geo = j.get("jobGeo", "Remote")

                if term_lower and not any(
                    kw in title_lower for kw in term_lower.split()
                ):
                    continue

                is_ok, _ = scope.is_compatible(job_geo, clean_desc)
                if not is_ok:
                    continue

                matched_jobs.append(
                    {
                        "site": "jobicy_remote",
                        "company": j.get("companyName", "Unknown"),
                        "title": title,
                        "location": job_geo,
                        "job_url": j.get("url", ""),
                        "description": clean_desc[:4000],
                    }
                )
    except requests.RequestException:
        pass

    return pd.DataFrame(matched_jobs)
