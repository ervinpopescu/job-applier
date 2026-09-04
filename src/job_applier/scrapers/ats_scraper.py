from __future__ import annotations

import re

import pandas as pd
import requests

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
)
from job_applier.config import (  # type: ignore[import-not-found]
    get_ats_companies_config,
)
from job_applier.scrapers.region_config import (  # type: ignore[import-not-found]
    RegionScope,
    get_default_region_scope,
)

GREENHOUSE_COMPANIES: list[str] = get_ats_companies_config().get("greenhouse", [])
LEVER_COMPANIES: list[str] = get_ats_companies_config().get("lever", [])


def scrape_greenhouse_jobs(
    search_term: str = "Cloud",
    location: str = "",
    companies: list[str] | None = None,
    region_scope: RegionScope | None = None,
    emea_only: bool = True,
) -> pd.DataFrame:
    """Scrapes public Greenhouse job boards via direct official JSON API filtered by region scope."""
    scope = region_scope or (
        get_default_region_scope() if emea_only else RegionScope(region="GLOBAL")
    )
    profile = load_candidate_profile()
    target_companies = companies or profile.greenhouse_companies or GREENHOUSE_COMPANIES
    term_lower = search_term.lower()
    loc_lower = location.lower()
    matched_jobs = []

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    for company in target_companies:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", company):
            continue
        url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code != 200:
                continue

            data = resp.json()
            jobs = data.get("jobs", [])

            for j in jobs:
                title = j.get("title", "")
                title_lower = title.lower()
                job_loc = (
                    j.get("location", {}).get("name", "")
                    if isinstance(j.get("location"), dict)
                    else ""
                )
                job_loc_lower = job_loc.lower()

                # Filter by keyword in title
                if term_lower and not any(
                    kw in title_lower for kw in term_lower.split()
                ):
                    continue

                # Filter by location if specified
                if (
                    loc_lower
                    and loc_lower not in job_loc_lower
                    and "remote" not in job_loc_lower
                ):
                    continue

                content = j.get("content", "")
                # Clean HTML tags from description
                clean_desc = re.sub(r"<[^>]+>", " ", content).strip()

                is_ok, _ = scope.is_compatible(job_loc or "Remote", clean_desc)
                if not is_ok:
                    continue

                matched_jobs.append(
                    {
                        "site": "greenhouse",
                        "company": company.capitalize(),
                        "title": title,
                        "location": job_loc or "Remote",
                        "job_url": j.get("absolute_url", ""),
                        "description": clean_desc[:4000],
                    }
                )

        except requests.RequestException:
            continue

    return pd.DataFrame(matched_jobs)


def scrape_lever_jobs(
    search_term: str = "Cloud",
    location: str = "",
    companies: list[str] | None = None,
    region_scope: RegionScope | None = None,
    emea_only: bool = True,
) -> pd.DataFrame:
    """Scrapes public Lever job boards via direct official JSON API filtered by region scope."""
    scope = region_scope or (
        get_default_region_scope() if emea_only else RegionScope(region="GLOBAL")
    )
    profile = load_candidate_profile()
    target_companies = companies or profile.lever_companies or LEVER_COMPANIES
    term_lower = search_term.lower()
    loc_lower = location.lower()
    matched_jobs = []

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    for company in target_companies:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", company):
            continue
        url = f"https://api.lever.co/v0/postings/{company}?mode=json"
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code != 200:
                continue

            postings = resp.json()
            if not isinstance(postings, list):
                continue

            for p in postings:
                title = p.get("text", "")
                title_lower = title.lower()
                job_loc = (
                    p.get("categories", {}).get("location", "")
                    if isinstance(p.get("categories"), dict)
                    else ""
                )
                job_loc_lower = job_loc.lower()

                if term_lower and not any(
                    kw in title_lower for kw in term_lower.split()
                ):
                    continue

                if (
                    loc_lower
                    and loc_lower not in job_loc_lower
                    and "remote" not in job_loc_lower
                ):
                    continue

                desc = p.get("descriptionPlain", "") or p.get("additionalPlain", "")

                is_ok, _ = scope.is_compatible(job_loc or "Remote", desc)
                if not is_ok:
                    continue

                matched_jobs.append(
                    {
                        "site": "lever",
                        "company": company.capitalize(),
                        "title": title,
                        "location": job_loc or "Remote",
                        "job_url": p.get("hostedUrl", ""),
                        "description": desc[:4000],
                    }
                )
        except requests.RequestException:
            continue

    return pd.DataFrame(matched_jobs)
