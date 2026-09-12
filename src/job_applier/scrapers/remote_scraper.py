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
    """
    Scrapes remote tech opportunities directly from employer listings via RemoteOK API.
    Jobicy is intentionally excluded because it serves indirect aggregator landing pages.
    """
    scope = region_scope or (
        get_default_region_scope() if emea_only else RegionScope(region="GLOBAL")
    )
    matched_jobs: list[dict[str, Any]] = []
    term_lower = (search_term or "").strip().lower()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    # Query RemoteOK API with optional tag filter
    try:
        tag_slug = term_lower.replace(" ", "-") if term_lower else ""
        url = (
            f"https://remoteok.com/api?tag={tag_slug}"
            if tag_slug
            else "https://remoteok.com/api"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    # Skip metadata / legal entry
                    if not item.get("id") or not item.get("company"):
                        continue

                    title = item.get("position", "")
                    title_lower = title.lower()
                    company = item.get("company", "Unknown")
                    location = item.get("location", "Remote") or "Remote"
                    desc = item.get("description", "")
                    clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()
                    tags = [
                        t.lower() for t in item.get("tags", []) if isinstance(t, str)
                    ]

                    # Validate regional scope (EMEA/location)
                    is_ok, _ = scope.is_compatible(location, clean_desc)
                    if not is_ok:
                        continue

                    # Filter by search_term in title, tags, or description
                    if term_lower:
                        term_words = term_lower.split()
                        matches_term = (
                            any(w in title_lower for w in term_words)
                            or any(w in tags for w in term_words)
                            or any(w in clean_desc.lower() for w in term_words)
                        )
                        if not matches_term:
                            continue

                    # Direct apply URL from RemoteOK
                    job_url = item.get("apply_url") or item.get("url") or ""
                    if not job_url:
                        continue

                    matched_jobs.append(
                        {
                            "site": "remoteok_remote",
                            "company": company,
                            "title": title,
                            "location": location,
                            "job_url": job_url,
                            "description": clean_desc[:4000],
                        }
                    )
                    if len(matched_jobs) >= count:
                        break
    except requests.RequestException:
        pass

    return pd.DataFrame(matched_jobs)
