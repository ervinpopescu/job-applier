from __future__ import annotations

import json
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from job_applier.config import get_job_filters_config

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

INACTIVE_PHRASES: list[str] = get_job_filters_config().get("inactive_phrases", [])


def check_bestjobs_activity(soup: BeautifulSoup, url: str) -> tuple[bool, str]:
    """Checks BestJobs Next.js SSR __NEXT_DATA__ and DOM for job active state."""
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
            page_props = data.get("props", {}).get("pageProps", {})
            job = page_props.get("job")
            if isinstance(job, dict):
                is_active = job.get("active")
                state = job.get("state", "").lower()
                if is_active is not None and not is_active:
                    return False, "BestJobs job is marked inactive (active=False)"
                if state in ["passive", "expired", "closed", "deleted", "archived"]:
                    return False, f"BestJobs job state is '{state}'"
        except Exception:
            pass

    # Check for apply buttons in DOM
    apply_btns = soup.find_all(
        lambda tag: (
            tag.name in ["button", "a"]
            and any(w in tag.text.lower() for w in ["aplică", "aplica", "apply"])
        )
    )
    if not apply_btns and "loc-de-munca" in url:
        return False, "BestJobs job page has no apply button (expired/passive)"

    return True, "Active"


def check_linkedin_activity(soup: BeautifulSoup, text_lower: str) -> tuple[bool, str]:
    """Checks LinkedIn public job page for closed indicators."""
    if (
        "no longer accepting applications" in text_lower
        or "înscrierile nu mai sunt acceptate" in text_lower
    ):
        return False, "LinkedIn posting is closed (no longer accepting applications)"

    # Check closed indicators in class names or aria-labels
    closed_indicators = soup.find_all(
        lambda tag: any(
            "closed" in str(tag.get(attr, "")).lower()
            for attr in ["class", "aria-label", "data-test"]
        )
    )
    for ind in closed_indicators:
        txt = ind.text.strip().lower()
        if "closed" in txt or "no longer" in txt:
            return False, "LinkedIn closed badge detected"

    return True, "Active"


def check_generic_ats_activity(
    resp: requests.Response, text_lower: str
) -> tuple[bool, str]:
    """Checks common ATS platforms (Greenhouse, Lever, Workday, SmartRecruiters)."""
    if resp.status_code in [404, 410]:
        return False, f"HTTP {resp.status_code}: Job posting deleted or closed"

    for phrase in INACTIVE_PHRASES:
        if phrase in text_lower:
            return False, f"Detected closure notice: '{phrase}'"

    return True, "Active"


def is_job_active(
    url: str,
    html: str | None = None,
    timeout: int = 8,
) -> tuple[bool, str]:
    """
    Predicts and verifies whether a job listing is genuinely active and accepting applications.
    Detects expired BestJobs (passive/active=False), closed LinkedIn posts, expired Indeed ads,
    and 404/closed ATS postings.
    Returns (is_active: bool, reason: str).
    """
    clean_url = url.strip()
    if not clean_url or not clean_url.startswith("http"):
        return False, "Invalid URL"

    # Fast domain parse
    domain = urlparse(clean_url).netloc.lower()

    # If HTML was already provided (e.g. from scraper or Playwright)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        text_lower = soup.get_text(separator=" ").lower()
    else:
        try:
            resp = requests.get(
                clean_url, headers=HEADERS, timeout=timeout, allow_redirects=True
            )
        except requests.RequestException as e:
            return False, f"Connection failed: {e}"

        if resp.status_code in [404, 410]:
            return False, f"HTTP {resp.status_code}: Page no longer exists"

        # Check if redirected away from job to home or generic search page
        final_url_path = urlparse(resp.url).path.lower()
        if final_url_path in ["", "/", "/ro", "/en", "/locuri-de-munca", "/jobs"]:
            return False, "Redirected to search homepage (job no longer available)"

        soup = BeautifulSoup(resp.text, "html.parser")
        text_lower = soup.get_text(separator=" ").lower()

    # 1. BestJobs specialized check
    if "bestjobs.eu" in domain:
        bj_ok, bj_reason = check_bestjobs_activity(soup, clean_url)
        if not bj_ok:
            return False, bj_reason

    # 2. LinkedIn specialized check
    if "linkedin.com" in domain:
        li_ok, li_reason = check_linkedin_activity(soup, text_lower)
        if not li_ok:
            return False, li_reason

    # 3. Generic ATS and portal inactive phrase scanning
    for phrase in INACTIVE_PHRASES:
        if phrase in text_lower:
            # Verify it's not in an unrelated disclaimer
            return False, f"Page contains closure notice: '{phrase}'"

    return True, "Active"
