from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

from job_applier.config import get_job_filters_config

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
}


def clean_text(text: str) -> str:
    """Removes extra whitespace and newlines."""
    return re.sub(r"\s+", " ", text).strip()


NON_TECHNICAL_EXCLUSIONS: set[str] = set(
    get_job_filters_config().get("non_technical_exclusions", [])
)


def title_matches_query(title: str, query: str) -> bool:
    """Rigorous case-insensitive keyword filtering to prevent sidebar ads / irrelevant recommendations."""
    clean_title = title.lower()
    clean_query = query.lower()

    # Reject non-technical roles unless explicitly requested by search query
    for exc in NON_TECHNICAL_EXCLUSIONS:
        if exc in clean_title and exc not in clean_query:
            return False

    # Split query into words and clean them
    words = [w.strip().lower() for w in re.split(r"[^a-zA-Z0-9]+", query) if w.strip()]
    # Exclude common noise words
    stop_words = {"and", "or", "for", "the", "with", "in", "ro", "of", "de"}
    keywords = [w for w in words if w not in stop_words and len(w) > 2]

    # Special abbreviation matching (e.g., 'QA' -> 'quality assurance', 'SRE' -> 'reliability')
    if "qa" in words and "quality" in clean_title:
        return True
    if "dev" in words and any(
        w in clean_title
        for w in ["developer", "development", "software", "programmer", "engineer"]
    ):
        return True

    if not keywords:
        return True  # Default to True if query is empty or too short

    # If query contains specific technical domain terms, require at least one to be in title
    domain_keywords = [
        w
        for w in keywords
        if w
        not in ["engineer", "developer", "specialist", "analyst", "manager", "inginer"]
    ]
    if domain_keywords:
        return any(dk in clean_title for dk in domain_keywords)

    # Require at least one significant keyword to be present in the job title
    return any(kw in clean_title for kw in keywords)


def scrape_ejobs(
    search_term: str = "Cloud",
    location: str = "Bucuresti",
    limit: int = 25,
) -> pd.DataFrame:
    """Scrapes job listings from eJobs (ejobs.ro)."""
    jobs: list[dict[str, Any]] = []
    base_url = "https://www.ejobs.ro/locuri-de-munca/"
    params = {"criteriu": search_term, "oras": location}

    try:
        resp = requests.get(base_url, params=params, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_urls = set()

            for a in soup.find_all("a"):
                raw_href = a.get("href")
                if (
                    not isinstance(raw_href, str)
                    or "/user/locuri-de-munca/" not in raw_href
                ):
                    continue

                href: str = raw_href
                title = clean_text(a.text)
                if not title or href in seen_urls:
                    continue

                if not title_matches_query(title, search_term):
                    continue

                seen_urls.add(href)
                full_url = (
                    href if href.startswith("http") else f"https://www.ejobs.ro{href}"
                )

                # Extract company name from card
                card = a.find_parent("div", class_="job-card-content") or a.find_parent(
                    "div"
                )
                company = "eJobs Employer"
                if card:
                    parts = [
                        clean_text(s)
                        for s in card.stripped_strings
                        if clean_text(s) and clean_text(s) != title
                    ]
                    for p in parts:
                        if not any(
                            w in p.lower()
                            for w in [
                                "sept",
                                "oct",
                                "nov",
                                "dec",
                                "ian",
                                "feb",
                                "mar",
                                "apr",
                                "mai",
                                "iun",
                                "iul",
                                "aug",
                                "202",
                                "bucurești",
                            ]
                        ):
                            company = p
                            break

                jobs.append(
                    {
                        "site": "ejobs",
                        "company": company,
                        "title": title,
                        "location": location,
                        "job_url": full_url,
                        "description": f"{title} at {company} in {location}. Full details on eJobs.",
                    }
                )

                if len(jobs) >= limit:
                    break

    except requests.RequestException as e:
        print(f"Notice: eJobs scraping error: {e}")

    return pd.DataFrame(jobs)


def scrape_bestjobs(
    search_term: str = "Cloud",
    location: str = "Bucuresti",
    limit: int = 25,
) -> pd.DataFrame:
    """Scrapes active job listings from BestJobs (bestjobs.eu) using Next.js SSR state and DOM fallback."""
    jobs: list[dict[str, Any]] = []
    base_url = "https://www.bestjobs.eu/ro/locuri-de-munca"
    params = {"keyword": search_term, "location": location}

    try:
        resp = requests.get(base_url, params=params, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_urls = set()

            # 1. Prefer BestJobs Next.js SSR state for 100% accurate active status
            script = soup.find("script", id="__NEXT_DATA__")
            if script and script.string:
                try:
                    data = json.loads(script.string)
                    items = (
                        data.get("props", {})
                        .get("pageProps", {})
                        .get("jobListCardsFromServer", {})
                        .get("items", [])
                    )
                    for it in items:
                        # Strictly filter: job must be active and not passive/expired
                        if not it.get("active") or it.get("state") != "active":
                            continue

                        title = clean_text(it.get("title", ""))
                        if not title or not title_matches_query(title, search_term):
                            continue

                        slug = it.get("slug", "").strip()
                        if not slug:
                            continue

                        full_url = f"https://www.bestjobs.eu/loc-de-munca/{slug}"
                        if full_url in seen_urls:
                            continue
                        seen_urls.add(full_url)

                        company = clean_text(
                            it.get("companyName") or "BestJobs Employer"
                        )
                        direct_url = (
                            it.get("ownApplyUrl") if it.get("hasOwnApplyUrl") else ""
                        )

                        jobs.append(
                            {
                                "site": "bestjobs",
                                "company": company,
                                "title": title,
                                "location": location,
                                "job_url": full_url,
                                "job_url_direct": direct_url or "",
                                "description": f"{title} at {company}. View full job description on BestJobs.",
                            }
                        )

                        if len(jobs) >= limit:
                            break
                except Exception as parse_err:
                    print(f"Notice parsing BestJobs SSR data: {parse_err}")

            # 2. Fallback to DOM elements if SSR state returned 0
            if not jobs:
                for a in soup.find_all("a"):
                    raw_href = a.get("href")
                    if (
                        not isinstance(raw_href, str)
                        or "/loc-de-munca/" not in raw_href
                    ):
                        continue

                    href: str = raw_href
                    title = clean_text(a.text)
                    if not title or href in seen_urls:
                        continue

                    if not title_matches_query(title, search_term):
                        continue

                    seen_urls.add(href)
                    full_url = (
                        href
                        if href.startswith("http")
                        else f"https://www.bestjobs.eu{href}"
                    )

                    # Find company name in card
                    card = a.find_parent("div", class_="relative") or a.find_parent(
                        "div"
                    )
                    company = "BestJobs Employer"
                    if card:
                        comp_div = card.find("div", class_="text-ink-medium")
                        if comp_div:
                            company = clean_text(comp_div.text)
                        else:
                            for s in card.stripped_strings:
                                st = clean_text(s)
                                if (
                                    st
                                    and st != title
                                    and len(st) < 40
                                    and not any(
                                        w in st.lower()
                                        for w in [
                                            "aplic",
                                            "rapid",
                                            "românia",
                                            "bucurești",
                                            "€",
                                            "lei",
                                        ]
                                    )
                                ):
                                    company = st
                                    break

                    jobs.append(
                        {
                            "site": "bestjobs",
                            "company": company,
                            "title": title,
                            "location": location,
                            "job_url": full_url,
                            "job_url_direct": "",
                            "description": f"{title} at {company}. View full job description on BestJobs.",
                        }
                    )

                    if len(jobs) >= limit:
                        break

    except requests.RequestException as e:
        print(f"Notice: BestJobs scraping error: {e}")

    return pd.DataFrame(jobs)


def scrape_hipo(
    search_term: str = "Cloud",
    location: str = "Bucuresti",
    limit: int = 25,
) -> pd.DataFrame:
    """Scrapes job listings from Hipo (hipo.ro)."""
    jobs: list[dict[str, Any]] = []
    loc_clean = re.sub(r"[^a-zA-Z0-9_\-]+", "", location) or "Bucuresti"
    term_clean = quote(search_term)
    url = f"https://www.hipo.ro/locuri-de-munca/cautacuvant/{loc_clean}/{term_clean}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_urls = set()

            for a in soup.find_all("a"):
                raw_href = a.get("href")
                if (
                    not isinstance(raw_href, str)
                    or "/locuri-de-munca/locuri_de_munca/" not in raw_href
                ):
                    continue

                href: str = raw_href
                title = clean_text(a.text)
                if (
                    not title
                    or href in seen_urls
                    or any(w in title.lower() for w in ["inscriere", "inscrie-te"])
                ):
                    continue

                if not title_matches_query(title, search_term):
                    continue

                seen_urls.add(href)
                full_url = (
                    href if href.startswith("http") else f"https://www.hipo.ro{href}"
                )

                company = "Hipo Employer"
                if "@" in title:
                    parts = title.split("@", 1)
                    title = parts[0].strip()
                    company = parts[1].strip()

                jobs.append(
                    {
                        "site": "hipo",
                        "company": company,
                        "title": title,
                        "location": location,
                        "job_url": full_url,
                        "description": f"{title} opportunity on Hipo in {location}.",
                    }
                )

                if len(jobs) >= limit:
                    break

    except requests.RequestException as e:
        print(f"Notice: Hipo scraping error: {e}")

    return pd.DataFrame(jobs)


def scrape_undelucram(
    search_term: str = "Cloud",
    location: str = "Bucuresti",
    limit: int = 25,
) -> pd.DataFrame:
    """Scrapes job listings from Undelucram (undelucram.ro)."""
    jobs: list[dict[str, Any]] = []
    base_url = "https://www.undelucram.ro/ro/locuri-de-munca"
    params = {"keyword": search_term, "location": location}

    try:
        resp = requests.get(base_url, params=params, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            seen_urls = set()

            for a in soup.find_all("a"):
                raw_href = a.get("href")
                if (
                    not isinstance(raw_href, str)
                    or "/ro/locuri-de-munca/" not in raw_href
                ):
                    continue

                href: str = raw_href
                raw_title = a.text.strip()
                title = clean_text(raw_title.split("\n")[0])
                if (
                    not title
                    or href in seen_urls
                    or "aplică" in title.lower()
                    or "caută" in title.lower()
                ):
                    continue

                if not title_matches_query(title, search_term):
                    continue

                seen_urls.add(href)
                full_url = (
                    href
                    if href.startswith("http")
                    else f"https://www.undelucram.ro{href}"
                )

                card = a.find_parent("div")
                company = "Undelucram Partner"
                if card:
                    parts = [
                        clean_text(s)
                        for s in card.stripped_strings
                        if clean_text(s) and clean_text(s) != title
                    ]
                    for p in parts:
                        if not any(
                            w in p.lower()
                            for w in [
                                "202",
                                "full-time",
                                "part-time",
                                "evaluări",
                                "remote",
                                "bucurești",
                                "aplică",
                            ]
                        ):
                            company = p
                            break

                jobs.append(
                    {
                        "site": "undelucram",
                        "company": company,
                        "title": title,
                        "location": location,
                        "job_url": full_url,
                        "description": f"{title} at {company}. View full requirements on Undelucram.",
                    }
                )

                if len(jobs) >= limit:
                    break

    except requests.RequestException as e:
        print(f"Notice: Undelucram scraping error: {e}")

    return pd.DataFrame(jobs)


def scrape_jooble(
    search_term: str = "Cloud",
    location: str = "Bucuresti",
    limit: int = 25,
    api_key: str | None = None,
) -> pd.DataFrame:
    """
    Queries Jooble API (if JOOBLE_API_KEY is available) or Jooble search feed.
    Get a free API key at https://jooble.org/api/about
    """
    jobs: list[dict[str, Any]] = []
    key = api_key or os.environ.get("JOOBLE_API_KEY", "").strip()

    if key and re.match(r"^[a-zA-Z0-9_\-]+$", key):
        url = f"https://jooble.org/api/{key}"
        payload = {"keywords": search_term, "location": location, "page": 1}
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                for j in data.get("jobs", [])[:limit]:
                    clean_desc = re.sub(r"<[^>]+>", " ", j.get("snippet", "")).strip()
                    jobs.append(
                        {
                            "site": "jooble",
                            "company": j.get("company", "Unknown"),
                            "title": j.get("title", search_term),
                            "location": j.get("location", location),
                            "job_url": j.get("link", ""),
                            "description": clean_desc,
                        }
                    )
        except requests.RequestException as e:
            print(f"Notice: Jooble API error: {e}")
    else:
        print(
            "  Notice: Jooble API requires JOOBLE_API_KEY. Add it to .env to enable direct Jooble integration."
        )

    return pd.DataFrame(jobs)
