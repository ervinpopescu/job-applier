from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from job_applier.tracker import (
    detect_platform_from_url,  # type: ignore[import-not-found]
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def is_direct_ats_url(url: str) -> bool:
    """Checks if a URL already points directly to a known ATS or corporate careers page."""
    u = url.lower()
    ats_indicators = [
        "greenhouse.io",
        "lever.co",
        "myworkdayjobs.com",
        "ashbyhq.com",
        "smartrecruiters.com",
        "careers.",
        "jobs.",
        "/careers/",
        "/career/",
        "/jobs/",
    ]
    return any(ind in u for ind in ats_indicators) and not any(
        agg in u
        for agg in [
            "indeed.com",
            "linkedin.com/jobs/view",
            "bestjobs.eu",
            "ejobs.ro",
            "undelucram.ro",
            "jooble.org",
            "jobicy.com",
            "jobicy.",
        ]
    )


def extract_redirect_target(url: str) -> str | None:
    """Extracts target URL embedded inside redirect query parameters."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ["url", "target", "redirect", "dest", "targetUrl", "jobUrl", "u"]:
        if key in params and params[key]:
            cand = params[key][0].strip()
            if cand.startswith("http"):
                return cand
    return None


def resolve_application_url(
    job_url: str,
    job_url_direct: str | None = None,
    timeout: int = 8,
) -> tuple[str, str]:
    """
    Resolves the actual direct company application form URL.
    Bypasses intermediary job boards (Undelucram, Indeed redirect wrappers, BestJobs external links).
    Returns (canonical_url, platform).
    """
    clean_url = job_url.strip()
    direct_url = (job_url_direct or "").strip()

    # 1. If job_url_direct is already a valid direct ATS URL, use it
    if direct_url and direct_url.startswith("http") and is_direct_ats_url(direct_url):
        return direct_url, detect_platform_from_url(direct_url)

    # 2. Check for embedded redirect query parameters
    embedded = extract_redirect_target(clean_url)
    if embedded and is_direct_ats_url(embedded):
        return embedded, detect_platform_from_url(embedded)

    # 3. If it's Undelucram, scrape the outbound 'Aplică extern' link
    if "undelucram.ro" in clean_url.lower():
        try:
            resp = requests.get(clean_url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    raw_href = a.get("href")
                    if not isinstance(raw_href, str):
                        continue
                    text = a.text.strip().lower()
                    href = raw_href.strip()
                    if (
                        "aplică extern" in text or "aplica extern" in text
                    ) and href.startswith("http"):
                        return href, detect_platform_from_url(href)
        except Exception:
            pass

    # 4. If it's an Indeed click wrapper or redirect, follow HTTP redirects
    if (
        "indeed.com/rc/clk" in clean_url.lower()
        or "indeed.com/company/" in clean_url.lower()
    ):
        try:
            resp = requests.head(
                clean_url, headers=HEADERS, allow_redirects=True, timeout=timeout
            )
            final_url = resp.url
            if (
                final_url
                and final_url.startswith("http")
                and "indeed.com" not in final_url
            ):
                return final_url, detect_platform_from_url(final_url)
        except Exception:
            pass

    # 5. Fallback: return direct_url if present, else original job_url
    effective_url = (
        direct_url if direct_url and direct_url.startswith("http") else clean_url
    )
    platform = detect_platform_from_url(effective_url)
    return effective_url, platform
