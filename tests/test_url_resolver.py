from __future__ import annotations

from unittest.mock import MagicMock, patch

from job_applier.scrapers.url_resolver import (
    extract_redirect_target,
    is_direct_ats_url,
    resolve_application_url,
)


def test_is_direct_ats_url() -> None:
    assert is_direct_ats_url("https://boards.greenhouse.io/datadog/jobs/123") is True
    assert is_direct_ats_url("https://jobs.lever.co/dlocal/abc-123") is True
    assert (
        is_direct_ats_url("https://capgemini.myworkdayjobs.com/careers/job/1") is True
    )
    assert (
        is_direct_ats_url("https://careers.capgemini.com/job/Junior-Data-Engineer/123")
        is True
    )
    assert is_direct_ats_url("https://ro.indeed.com/viewjob?jk=123") is False
    assert is_direct_ats_url("https://www.linkedin.com/jobs/view/12345") is False
    assert is_direct_ats_url("https://www.bestjobs.eu/loc-de-munca/123") is False


def test_extract_redirect_target() -> None:
    wrapped = "https://ro.indeed.com/rc/clk?url=https%3A%2F%2Fjobs.lever.co%2Fdlocal%2F123&jk=abc"
    target = extract_redirect_target(wrapped)
    assert target == "https://jobs.lever.co/dlocal/123"

    non_wrapped = "https://example.com/job/123"
    assert extract_redirect_target(non_wrapped) is None


def test_resolve_application_url_direct_ats() -> None:
    # 1. Directly resolves when job_url_direct is already an ATS
    raw = "https://ro.indeed.com/viewjob?jk=abc"
    direct = "https://jobs.lever.co/dlocal/abc-123"
    resolved, platform = resolve_application_url(raw, direct)
    assert resolved == direct
    assert platform == "Lever"


def test_resolve_application_url_undelucram_scraping() -> None:
    # 2. Scrapes outbound external apply link from Undelucram HTML
    mock_html = """
    <html>
        <body>
            <a class="btn" href="https://careers.capgemini.com/job/DevOps/999">Aplică extern</a>
        </body>
    </html>
    """
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = mock_html
        mock_get.return_value = mock_resp

        resolved, platform = resolve_application_url(
            "https://www.undelucram.ro/ro/locuri-de-munca/devops/123"
        )
        assert resolved == "https://careers.capgemini.com/job/DevOps/999"
        assert platform == "Capgemini"
